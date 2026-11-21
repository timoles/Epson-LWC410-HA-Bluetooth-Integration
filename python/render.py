#!/usr/bin/env python3
"""Label templates for the Epson LW-C410: data -> 1-bit label image -> printer.

Renders labels with Pillow (TrueType text, no anti-aliasing) and segno (QR
codes), then hands them to lwc410_bt.print_bitmap(). Every template returns a
Pillow image in *natural orientation*: width = along the tape (reading
direction), height = printable dots across the tape (12 mm -> 72, 18 mm -> 96).
Black (luma < 140) = printed dot.

Templates:
    render_text(lines, tape_mm)                 one or more text lines
    render_qr(data, tape_mm, lines)             QR code + text lines
    render_homebox(base_url, tape_mm, ...)      Homebox asset/item QR + name/location

QR sizing: whole dots per module when 3 or more fit (3 dots = 0.42 mm, verified
on hardware), otherwise fractional scaling to the tape width (see make_qr);
below 2 dots/module the payload is rejected. Whole 3-dot modules fit QR version
1 on 12 mm tape (17 bytes, or 25 characters of 0-9 A-Z $%*+-./: space) and
version 3 on 18 mm tape (53 bytes). A 30-36 char https URL needs version 2-3:
~2.6 dots/module on 12 mm, 3 dots on 18 mm.

CLI (needs the `render` extra: `uv run --extra render render.py ...`):
    render.py text "Garage" "Shelf 3" --tape 12 -o label.png --preview
    render.py homebox --base-url https://hb.lan --asset-id 42 --name "Drill" \\
        --location "Garage" --tape 18 -o label.png
    render.py homebox ... --print --mac AA:BB:CC:DD:EE:FF        (Linux, BlueZ)
"""

import argparse
import functools
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lwc410_bt as lw  # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - depends on the environment
    raise SystemExit("render.py needs Pillow and segno: uv run --extra render render.py ...")
try:
    import segno
except ImportError:  # pragma: no cover
    raise SystemExit("render.py needs segno: uv run --extra render render.py ...")


WHITE = 255
BLACK = 0
MIN_QR_MODULE = 2           # dots per module; below this phones will not scan it
GOOD_QR_MODULE = 3          # 0.42 mm modules: verified on the LW-C410 (12 mm, v1)
QR_EDGE_DOTS = 3            # blank dots across the tape on each side of a scaled QR
QR_LEAD_MODULES = 2         # blank before the QR; the print margin adds more
QR_GAP_MODULES = 4          # blank between QR and text (QR quiet zone)
LINE_GAP = 2                # dots between text lines
DETAIL_RATIO = 0.19         # Homebox labels: share of the tape height for the
                            # asset id + location row (half the former font size)
EDGE_PAD = 1                # dots kept blank at the printable edges across the tape
END_PAD = 4                 # dots after the last text column
DEFAULT_MAX_LENGTH_MM = 100.0
# Probe string for font sizing: tallest ascender + deepest descender.
SIZE_PROBE = "ÅÄHÖgjpqy|"

FONT_CANDIDATES = (
    # Linux (Debian/Ubuntu/Alpine fonts-dejavu)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)


class RenderError(ValueError):
    """Content does not fit the tape (too long a QR payload, empty text, ...)."""


# ---------------------------------------------------------------------------
# Fonts and text
# ---------------------------------------------------------------------------

def default_font_path():
    """$LWC410_FONT, else the first bold sans font found, else None (Pillow's)."""
    env = os.environ.get("LWC410_FONT")
    if env:
        return env
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


@functools.lru_cache(maxsize=256)
def load_font(size, path=None):
    size = max(4, int(size))
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError as exc:
            raise RenderError(f"cannot load font {path}: {exc}")
    try:
        return ImageFont.load_default(size)        # Pillow >= 10.1, FreeType
    except TypeError:                              # pragma: no cover - old Pillow
        raise RenderError("no TrueType font found; install fonts-dejavu or set "
                          "LWC410_FONT=/path/to/font.ttf (or upgrade Pillow)")


@functools.lru_cache(maxsize=256)
def fit_font(height, path=None):
    """Largest font whose ascender-to-descender box fits `height` dots."""
    if height < 5:
        raise RenderError(f"text line of {height} dots is too small to be legible")
    lo, hi, best = 4, int(height * 2), None
    while lo <= hi:
        mid = (lo + hi) // 2
        font = load_font(mid, path)
        _, top, _, bottom = font.getbbox(SIZE_PROBE, anchor="ls")
        if bottom - top <= height:
            best, lo = font, mid + 1
        else:
            hi = mid - 1
    if best is None:
        raise RenderError(f"no font size fits a {height}-dot line")
    return best


def text_width(font, text):
    left, _, right, _ = font.getbbox(text, anchor="ls")
    return max(0, right - min(0, left))


def truncate(font, text, max_width):
    """Shorten `text` with an ellipsis until it fits `max_width` dots."""
    if max_width is None or text_width(font, text) <= max_width:
        return text
    while text and text_width(font, text + "…") > max_width:
        text = text[:-1].rstrip()
    return text + "…" if text else ""


def clean_lines(lines):
    out = []
    for line in lines:
        if line is None:
            continue
        text = re.sub(r"\s+", " ", str(line)).strip()
        # drop control characters (labels come from HTTP/HA input)
        text = "".join(ch for ch in text if ch.isprintable())
        if text:
            out.append(text)
    return out


def line_heights(count, height, emphasize_first=True):
    """Split `height` dots into `count` line boxes; the first line gets more."""
    usable = height - 2 * EDGE_PAD - (count - 1) * LINE_GAP
    weights = [1.0] + [0.7] * (count - 1) if emphasize_first else [1.0] * count
    total = sum(weights)
    heights = [int(usable * w / total) for w in weights]
    heights[0] += usable - sum(heights)                # rounding rest to line 1
    return heights


def text_block(lines, height, max_width=None, font_path=None, emphasize_first=True,
               heights=None):
    """Render text lines stacked across the tape; returns an 'L' image.

    heights: explicit row heights in dots (one per line); default splits the
    tape height with the first line emphasized. Rows are centred vertically.
    """
    if heights is not None:
        if len(heights) != len(lines):
            raise ValueError("one height per line required")
        pairs = []
        for text, h in zip(lines, heights):
            cleaned = clean_lines([text])
            if cleaned:                      # drop empty rows with their height
                pairs.append((cleaned[0], h))
        lines = [text for text, _ in pairs]
        heights = [h for _, h in pairs]
    else:
        lines = clean_lines(lines)
    if not lines:
        raise RenderError("no text to print")
    if heights is None:
        heights = line_heights(len(lines), height, emphasize_first)
    if min(heights) < 5:
        raise RenderError(f"{len(lines)} lines do not fit {height} dots across the "
                          "tape; use fewer lines or a wider tape")
    fonts = [fit_font(h, font_path) for h in heights]
    lines = [truncate(f, t, max_width) for f, t in zip(fonts, lines)]
    width = max(1, max(text_width(f, t) for f, t in zip(fonts, lines)))

    image = Image.new("L", (width, height), WHITE)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"                     # no anti-aliasing: crisp 1-bit glyphs
    used = sum(heights) + (len(heights) - 1) * LINE_GAP
    y = EDGE_PAD + max(0, (height - 2 * EDGE_PAD - used) // 2)
    for font, text, h in zip(fonts, lines, heights):
        _, top, _, _ = font.getbbox(SIZE_PROBE, anchor="ls")
        left = min(0, font.getbbox(text, anchor="ls")[0])
        draw.text((-left, y - top), text, font=font, fill=BLACK, anchor="ls")
        y += h + LINE_GAP
    return image


def wrap_balanced(font, text):
    """Split text at a space into two rows of as equal width as possible."""
    words = text.split()
    if len(words) < 2:
        return [text]
    best = None
    for i in range(1, len(words)):
        rows = [" ".join(words[:i]), " ".join(words[i:])]
        width = max(text_width(font, r) for r in rows)
        if best is None or width < best[0]:
            best = (width, rows)
    return best[1]


def name_detail_layout(name, detail, dots, font_path=None):
    """Name wrapped onto two rows, a small detail row below.

    Saves label length: the name is set in two half-height rows instead of one
    long line, and the detail row (asset id + location) gets DETAIL_RATIO of
    the tape height. Returns (lines, heights) for text_block().
    """
    name = " ".join(str(name or "").split()) or "(unnamed)"
    detail = " ".join(str(detail or "").split())
    detail_h = max(5, round(dots * DETAIL_RATIO)) if detail else 0
    usable = dots - 2 * EDGE_PAD - detail_h - (LINE_GAP if detail else 0)
    row_h = (usable - LINE_GAP) // 2
    rows = wrap_balanced(fit_font(row_h, font_path), name)
    lines = rows + ([detail] if detail else [])
    heights = [row_h] * len(rows) + ([detail_h] if detail else [])
    return lines, heights


# ---------------------------------------------------------------------------
# QR codes
# ---------------------------------------------------------------------------

def make_qr(data, dots, error="l"):
    """segno QR (no micro) scaled as large as fits `dots` across the tape.

    Whole dots per module when >= 3 fit (sharpest). Otherwise the code is
    scaled fractionally to dots - 2*QR_EDGE_DOTS: module edges are rounded to
    the dot grid (Bresenham-style), so every module is 2 or 3 dots wide and no
    edge is more than half a dot off the ideal grid -- well within what QR
    decoders tolerate, and e.g. 2.64 instead of 2 dots/module for a version-2
    code on 12 mm tape.

    Returns (image, module_dots as float, version). boost_error raises the
    error correction level for free when the payload leaves room.
    """
    if not data:
        raise RenderError("empty QR payload")
    qr = segno.make(data, error=error, micro=False, boost_error=True)
    size = qr.symbol_size(border=0)[0]
    if dots // size >= GOOD_QR_MODULE:
        px = (dots // size) * size
    else:
        px = dots - 2 * QR_EDGE_DOTS
    module = px / size
    if module < MIN_QR_MODULE:
        raise RenderError(
            f"QR payload too long for this tape: {len(data)} chars need version "
            f"{qr.version} ({size}x{size} modules), {dots} dots across allow at most "
            f"{(dots - 2 * QR_EDGE_DOTS) // MIN_QR_MODULE} modules. Use a shorter URL "
            "(asset id, short host name), the uppercase option, or a wider tape.")
    edges = [round(i * px / size) for i in range(size + 1)]
    image = Image.new("L", (px, px), WHITE)
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(qr.matrix_iter(scale=1, border=0)):
        for x, dark in enumerate(row):
            if dark:
                draw.rectangle((edges[x], edges[y], edges[x + 1] - 1, edges[y + 1] - 1),
                               fill=BLACK)
    return image, module, qr.version


def qr_payload(data, uppercase=False):
    """Optionally upper-case the payload so segno can use alphanumeric mode.

    Alphanumeric mode stores 0-9 A-Z space $%*+-./: at 5.5 bits/char instead of
    8, which makes e.g. HTTP://HB.LAN/A/000-042 (24 chars) fit QR version 1 on
    12 mm tape. Only safe when the server treats the URL case-insensitively.
    """
    return data.upper() if uppercase else data


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _max_width(max_length_mm, used):
    if max_length_mm is None:
        return None
    return max(10, lw.mm_to_dots(max_length_mm) - used)


def _canvas(width, height):
    return Image.new("L", (max(1, width), height), WHITE)


def render_text(lines, tape_mm, length_mm=None, max_length_mm=DEFAULT_MAX_LENGTH_MM,
                font_path=None, emphasize_first=True):
    """Text-only label. length_mm fixes the length (text centred)."""
    dots = lw.printable_dots(tape_mm)
    font_path = font_path or default_font_path()
    if length_mm is not None:
        max_length_mm = length_mm
    block = text_block(lines, dots, _max_width(max_length_mm, 2 * END_PAD),
                       font_path, emphasize_first)
    width = block.width + 2 * END_PAD
    if length_mm is not None:
        width = max(width, lw.mm_to_dots(length_mm))
    image = _canvas(width, dots)
    image.paste(block, ((width - block.width) // 2, 0))
    return image


def render_qr(data, tape_mm, lines=(), max_length_mm=DEFAULT_MAX_LENGTH_MM,
              font_path=None, uppercase=False, error="l", heights=None):
    """QR code at the leading end, optional text lines after it.

    heights: explicit text row heights (see text_block), e.g. from
    name_detail_layout().

    Returns (image, info) with info = {"module_dots", "version", "payload"}.
    """
    dots = lw.printable_dots(tape_mm)
    font_path = font_path or default_font_path()
    payload = qr_payload(data, uppercase)
    qr, module, version = make_qr(payload, dots, error=error)
    lead = round(QR_LEAD_MODULES * module)
    x_text = lead + qr.width + round(QR_GAP_MODULES * module)

    block = None
    if clean_lines(lines):
        block = text_block(lines, dots, _max_width(max_length_mm, x_text + END_PAD),
                           font_path, heights=heights)
    width = (x_text + block.width + END_PAD) if block else (2 * lead + qr.width)
    image = _canvas(width, dots)
    image.paste(qr, (lead, (dots - qr.height) // 2))
    if block:
        image.paste(block, (x_text, 0))
    return image, {"module_dots": module, "version": version, "payload": payload}


ASSET_ID_RE = re.compile(r"^\d{1,6}$|^\d{3}-\d{3}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def format_asset_id(value):
    """42, '42', '000042' or '000-042' -> '000-042' (Homebox display format)."""
    text = str(value).strip()
    if not ASSET_ID_RE.match(text):
        raise RenderError(f"invalid Homebox asset id {value!r} (expected e.g. 000-042)")
    digits = text.replace("-", "").zfill(6)
    if int(digits) == 0:
        raise RenderError("asset id 000-000 means 'no asset id' in Homebox")
    return f"{digits[:3]}-{digits[3:]}"


ITEM_URL_RE = re.compile(r"/(?:item|location)/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                         r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?$")
ASSET_URL_RE = re.compile(r"/a/(\d{3}-\d{3}|\d{1,6})/?$", re.IGNORECASE)
FIELD_LABEL_RE = re.compile(r"^[^:/]{1,24}:\s+(\S.*)$")


def is_asset_url(url):
    """True for Homebox /a/<asset id> URLs (verified case-insensitive in v0.27)."""
    return bool(ASSET_URL_RE.search(url or ""))


def strip_field_label(text):
    """'Location: Garage' -> 'Garage' (Homebox label-service description lines)."""
    match = FIELD_LABEL_RE.match(text or "")
    return match.group(1) if match else text


def homebox_url(base_url, asset_id=None, item_id=None):
    """URL the QR code points to: /a/<asset id> (short) or /item/<uuid>."""
    base = (base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", base, re.IGNORECASE):
        raise RenderError(f"invalid Homebox base URL {base_url!r} "
                          "(expected e.g. https://homebox.lan)")
    if asset_id is not None and str(asset_id).strip():
        return f"{base}/a/{format_asset_id(asset_id)}"
    if item_id:
        if not UUID_RE.match(str(item_id)):
            raise RenderError(f"invalid Homebox item id {item_id!r} (expected a UUID)")
        return f"{base}/item/{str(item_id).lower()}"
    raise RenderError("need an asset id or an item id")


def render_homebox(base_url, tape_mm, asset_id=None, item_id=None, name="",
                   location=None, max_length_mm=DEFAULT_MAX_LENGTH_MM,
                   font_path=None, uppercase=False):
    """Homebox label: QR (-> asset/item page), name, then asset id + location.

    Asset ids give short URLs that fit small QR codes; item UUID URLs (~60
    chars) generally do not fit even 18 mm tape at a scannable module size.
    """
    url = homebox_url(base_url, asset_id=asset_id, item_id=item_id)
    second = []
    if asset_id is not None and str(asset_id).strip():
        second.append(format_asset_id(asset_id))
    if location:
        second.append(str(location))
    dots = lw.printable_dots(tape_mm)
    lines, heights = name_detail_layout(name, "  ".join(second), dots,
                                        font_path or default_font_path())
    image, info = render_qr(url, tape_mm, lines, max_length_mm=max_length_mm,
                            font_path=font_path, uppercase=uppercase, heights=heights)
    info["url"] = url
    return image, info


def fit_image(image, tape_mm, rotate=False, max_length_mm=200.0):
    """Scale an arbitrary image so its height spans the tape (aspect kept).

    rotate=True turns portrait images by 90 degrees first. Returns an 'L'
    image; thresholding happens in to_bitmap().
    """
    dots = lw.printable_dots(tape_mm)
    image = image.convert("L")
    if rotate:
        image = image.rotate(90, expand=True)
    if image.width < 1 or image.height < 1:
        raise RenderError("empty image")
    width = max(1, round(image.width * dots / image.height))
    if width > lw.mm_to_dots(max_length_mm):
        raise RenderError(f"image would be {width} dots long "
                          f"(> {max_length_mm:g} mm) on {tape_mm} mm tape")
    return image.resize((width, dots), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def to_bitmap(image):
    return lw.bitmap_from_image(image, threshold=140)


def ascii_preview(image):
    gray = image.convert("L")
    width, height = gray.size
    data = gray.tobytes()
    return "\n".join(
        "".join("#" if data[y * width + x] < 140 else "." for x in range(width))
        for y in range(height))


def save_png(image, path, scale=1):
    out = image.convert("1")
    if scale > 1:
        out = out.resize((out.width * scale, out.height * scale), Image.NEAREST)
    out.save(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Render (and optionally print) LW-C410 labels.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="template", required=True)

    def common(p):
        p.add_argument("--tape", type=int, choices=lw.TAPE_CHOICES, default=12,
                       metavar="MM", help="tape width in mm (default 12)")
        p.add_argument("--font", metavar="TTF", help="TrueType font (default: "
                       "$LWC410_FONT or a bold system sans)")
        p.add_argument("--max-length-mm", type=float, default=DEFAULT_MAX_LENGTH_MM,
                       help="truncate text so the label stays below this length")
        p.add_argument("-o", "--output", metavar="PNG", help="save a PNG preview")
        p.add_argument("--scale", type=int, default=1, help="PNG preview scale")
        p.add_argument("--preview", action="store_true",
                       help="print an ASCII preview to the terminal")
        p.add_argument("--print", dest="do_print", action="store_true",
                       help="send the label to the printer")
        p.add_argument("--dry-run", action="store_true",
                       help="with --print: build the job, do not connect")
        p.add_argument("--mac", help="printer Bluetooth address (Linux)")
        p.add_argument("--port", help="serial port (macOS / bridges)")
        p.add_argument("--margin-mm", type=float, default=lw.LIB_MARGIN_MM,
                       help=f"blank tape at each end (default {lw.LIB_MARGIN_MM:g})")
        p.add_argument("--copies", type=int, default=1)
        p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("text", help="text lines")
    p.add_argument("lines", nargs="+")
    p.add_argument("--length-mm", type=float, help="fixed label length")
    common(p)

    p = sub.add_parser("qr", help="QR code + optional text lines")
    p.add_argument("data")
    p.add_argument("lines", nargs="*")
    p.add_argument("--upper", action="store_true",
                   help="upper-case the payload (alphanumeric QR mode, smaller code)")
    common(p)

    p = sub.add_parser("homebox", help="Homebox asset/item label")
    p.add_argument("--base-url", default=os.environ.get("HOMEBOX_URL"),
                   help="Homebox URL as reachable from your phone ($HOMEBOX_URL)")
    p.add_argument("--asset-id", help="Homebox asset id, e.g. 000-042 or 42")
    p.add_argument("--item-id", help="Homebox item UUID (long URL, large QR)")
    p.add_argument("--name", required=True)
    p.add_argument("--location")
    p.add_argument("--upper", action="store_true",
                   help="upper-case the URL (alphanumeric QR mode, smaller code)")
    common(p)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        info = {}
        if args.template == "text":
            image = render_text(args.lines, args.tape, length_mm=args.length_mm,
                                max_length_mm=args.max_length_mm, font_path=args.font)
        elif args.template == "qr":
            image, info = render_qr(args.data, args.tape, args.lines,
                                    max_length_mm=args.max_length_mm,
                                    font_path=args.font, uppercase=args.upper)
        else:
            image, info = render_homebox(
                args.base_url, args.tape, asset_id=args.asset_id,
                item_id=args.item_id, name=args.name, location=args.location,
                max_length_mm=args.max_length_mm, font_path=args.font,
                uppercase=args.upper)

        length_mm = image.width / (lw.DPI / lw.MM_PER_INCH) + 2 * args.margin_mm
        print(f"label: {image.width} x {image.height} dots, "
              f"~{length_mm:.1f} mm of tape incl. margins")
        if info:
            module = info["module_dots"]
            print(f"QR: version {info['version']}, {module:.2f} dots/module "
                  f"({module * lw.MM_PER_INCH / lw.DPI:.2f} mm), payload {info['payload']!r}")
            if module < GOOD_QR_MODULE:
                print("note: QR modules below 3 dots - test-scan the first label",
                      file=sys.stderr)
        if args.preview:
            print(ascii_preview(image))
        if args.output:
            save_png(image, args.output, args.scale)
            print(f"saved {args.output}")

        if args.do_print:
            logger = lw.Logger(args.verbose)
            bitmap = to_bitmap(image)
            if args.dry_run:
                plan = lw.plan_bitmap(bitmap, args.tape, margin_mm=args.margin_mm,
                                      copies=args.copies)
                lw.report_plan(plan, logger)
                print("Dry run: not connecting.")
            else:
                lw.print_bitmap(bitmap, args.tape, mac=args.mac, port=args.port,
                                logger=logger, margin_mm=args.margin_mm,
                                copies=args.copies)
        return 0
    except lw.ProtocolError as exc:
        print(f"protocol error: {exc}", file=sys.stderr)
        return 3
    except (lw.TransportError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
