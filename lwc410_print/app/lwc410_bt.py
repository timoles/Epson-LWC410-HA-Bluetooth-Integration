#!/usr/bin/env python3
"""Talk to an Epson LabelWorks LW-C410 label printer over Bluetooth Classic SPP.

Protocol reference: ../docs/PROTOCOL.md (frame grammar 1, BT link 2, session flow 3,
status frame 4, print stream 5, timeouts 8). This file is a port of the pure-protocol
parts of ../esphome/components/epson_lwc410/{protocol.h,printer.cpp,label_render.h}
to Python, plus a CLI for protocol debugging and standalone print jobs.

Two transports, no pip packages either way:

* Linux: Bluetooth Classic RFCOMM sockets via BlueZ (AF_BLUETOOTH). Pairing is
  not required -- the printer uses insecure RFCOMM, like the Android app.
* macOS: the OS has no RFCOMM socket API. Pair the printer once in System
  Settings -> Bluetooth, then drive the serial port macOS creates for it
  (/dev/cu.<name>) with --port. The same --port backend also works on Linux
  (USB adapters, ESP32 serial bridges, ptys).

PBM images are parsed in pure Python; PNG input is accepted only when Pillow
happens to be installed.

Examples:
    ./lwc410_bt.py scan
    ./lwc410_bt.py status  --mac AA:BB:CC:DD:EE:FF -v
    ./lwc410_bt.py monitor --mac AA:BB:CC:DD:EE:FF
    ./lwc410_bt.py feed    --mac AA:BB:CC:DD:EE:FF
    ./lwc410_bt.py raw     --mac AA:BB:CC:DD:EE:FF 1B7B0551050056 7D
    ./lwc410_bt.py print   --dry-run -p bars --tape 12 --length-mm 25
    ./lwc410_bt.py print   --mac AA:BB:CC:DD:EE:FF -p "text HELLO" --tape 12 -v
    ./lwc410_bt.py status  --port /dev/cu.LW-C410-SerialPort1 -v   # macOS

All connecting subcommands take either --mac (Linux RFCOMM; `scan` prints the
address) or --port PATH (macOS serial port, USB/ESP32 bridge). Global options
such as -v/--verbose and --timeout may be given before or after the subcommand.
Without -v only a short progress log is printed; with -v every read and write is
hexdumped with a direction arrow.

Exit codes: 0 ok, 1 error/usage, 2 no status frame, 3 printer reported an error.
"""

import argparse
import errno
import fcntl
import glob
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

try:                            # POSIX serial line settings; RFCOMM needs none
    import termios
except ImportError:             # pragma: no cover - non-POSIX platform
    termios = None

# ---------------------------------------------------------------------------
# Protocol constants (docs/PROTOCOL.md 1, 3, 5)
# ---------------------------------------------------------------------------

ESC = 0x1B
LBRACE = 0x7B
RBRACE = 0x7D

CMD_RESET = 0x21          # '!'  printer reset (BT path)
CMD_FEED = 0x2B           # '+'  feed (p=0) / feed+cut (p=1)
CMD_JOB_START = 0x40      # '@'  job/document start and print end
CMD_CUTTER = 0x43         # 'C'  cutter mode (job env)
CMD_DENSITY = 0x44        # 'D'  density (job env)
CMD_JOB_TRAILER = 0x47    # 'G'  job env trailer
CMD_PAGE_LENGTH = 0x4C    # 'L'  page length (page env)
CMD_STATUS = 0x51         # 'Q'  status notification on/off
CMD_MARGIN = 0x54         # 'T'  leading margin (page env)
CMD_JOB_INIT = 0x7B       # '{'  job init signature (fixed)

JOB_INIT_SIGNATURE = b"\x00\x00\x53\x54"   # 00 00 'S' 'T'
RASTER_HEADER_PREFIX = b"\x1b.\x00\x00\x00\x01"
END_OF_PAGE = b"\x0c"
JOB_SIGNATURE = b"\x00\x00\x53\x54"

CUT_VECTORS = {
    # docs/PROTOCOL.md 5.1 -- p2 p3 are always 01 01; only p0/p1 vary.
    "each": b"\x01\x01\x01\x01",   # cut after every label
    "job": b"\x01\x00\x01\x01",    # cut after the whole job
    "none": b"\x00\x00\x00\x00",   # no cut
}
CUT_LABELS = {
    "each": "each label",
    "job": "after job",
    "none": "no cut",
}

DPI = 180
MM_PER_INCH = 25.4
MAX_BAND_ROWS = 1024                # PrintController banding size (protocol limit)
DEFAULT_BAND_ROWS = 64              # [HW-verified 2026-11] single writes must stay small:
                                    # a 6324 B band (372 rows) lost the label tail; 64-row
                                    # bands (1088 B) print reliably and keep status alive
DEFAULT_MARGIN_MM = 10.0            # -> 70 dots at 180 dpi
CONNECT_TIMEOUT = 10.0              # per RFCOMM connect attempt
READ_TIMEOUT = 20.0                 # per status read
WRITE_TIMEOUT = 20.0                # normal write
BAND_WRITE_TIMEOUT = 60.0           # raster band write
BAND_STATUS_TIMEOUT = 5.0           # status frame after a band (warn only if absent)
BAND_DELAY_MS = 500                 # model 1300 pacing
IDLE_WAIT_TIMEOUT = 30.0            # post-job wait for idle; the app's read
                                    # timeout after a print is 30 s (PROTOCOL.md 4.1).
                                    # Closing the session at 10 s (old value) wedged the
                                    # LW-C410 whenever a long label was still feeding/cutting
                                    # past the cutoff (verified on hardware).
CONNECT_RETRY_DELAY = 0.5           # >= 500 ms between socket opens
SPP_CHANNEL_SNOOP = 6              # LW-C410 SPP server channel (HCI snoop, PROTOCOL.md 2)
LINK_SETTLE = 0.5                   # serial open -> first write (BT link comes up late)
STATUS_ATTEMPTS = 3                 # Q(05) send attempts in session_prepare
STATUS_ATTEMPT_WINDOW = 3.0         # seconds to wait per attempt for a frame
STATUS_FRAME_LEN = 64

# Printable dots across the tape at 180 dpi (docs/PROTOCOL.md 5.4).
# 18 mm is the model-1300 special case: 96 dots, not the generic 108.
TAPE_DOTS = {4: 22, 6: 36, 9: 54, 12: 72, 18: 96}
TAPE_CHOICES = sorted(TAPE_DOTS)

READY_STATES = (0x00, 0x04, 0x05)   # idle / feed end / print end
BUSY_STATES = (0x01, 0x02, 0x03)    # feeding / printing / data sending
IDLE_STATES = (0x00, 0x05)          # what idleWait() waits for (PROTOCOL.md 3.13)

MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Unicode-less status text tables (docs/PROTOCOL.md 4.2).
ST_TEXT = {
    0x00: "idle",
    0x01: "feeding",
    0x02: "printing",
    0x03: "data sending",
    0x04: "feed end",
    0x05: "print end",
    0x10: "demo",
    0x13: "firmware update",
    0x20: "small roll wait",
    0x22: "waiting tape removal",
    0xFF: "unexpected error",
}

ER_TEXT = {
    0x00: "none",
    0x01: "cutter error",
    0x06: "no cartridge",
    0x21: "head overheat",
    0x33: "cover open",
    0x34: "low voltage",
    0x35: "power-off cancel",
    0x36: "tape eject",
    0x40: "ribbon slack",
    0x41: "ribbon short",
    0x42: "tape end",
    0x43: "cut label error",
    0x44: "temperature error",
    0x48: "tape feed error",
    0xFF: "unexpected error",
}

# TW cartridge code -> tape width in mm (native tapeWidthFromLWStatusTWCode table).
TW_TAPE_MM = {
    0x00: 0,          # no cartridge
    0x01: 6, 0x51: 6,
    0x02: 9, 0x52: 9,
    0x03: 12, 0x53: 12,
    0x04: 18, 0x54: 18,
    0x05: 24, 0x55: 24,
    0x06: 36, 0x56: 36,
    0x07: 50, 0x57: 50,
    0x0B: 4, 0x5B: 4,
    0x11: 24,         # cable 24 mm
    0x12: 36,         # cable 36 mm
    0x21: 50,
    0x23: 100,
}

TR_TEXT = {
    0x00: "normal",
    0x01: "transfer",
    0x10: "cable",
    0x11: "index",
    0x40: "braille",
    0x50: "olefin",
    0x51: "thermal paper",
    0x52: "tube",
    0x53: "PET",
    0x70: "HST",
    0x80: "vinyl",
}

STATUS_KEYS = ("ST", "ER", "TW", "TR", "EI", "TO", "IR", "RR", "EJ",
               "WR", "RV", "CT", "OP")

# "KEY:hh" anywhere in the 62-byte body. The app uses lastIndexOf() per key;
# scanning all matches and letting the last one win reproduces that and also
# copes with frames whose fields are not newline-separated.
KEY_VALUE_RE = re.compile(r"([A-Za-z][A-Za-z0-9]{1,3}):([0-9A-Fa-f]{2})")

# 5x7 font, ASCII 0x20..0x7E, 5 column bytes per glyph, bit0 = top row
# (same table as esphome/components/epson_lwc410/font5x7.h, Adafruit glcdfont).
FONT5X7 = [
    bytes.fromhex(h) for h in """
    0000000000 00005f0000 0007000700 147f147f14 242a7f2a12 2313086462
    3649562050 0008070300 001c224100 0041221c00 2a1c7f1c2a 08083e0808
    0080703000 0808080808 0000606000 2010080402 3e5149453e 00427f4000
    7249494946 2141494d33 1814127f10 2745454539 3c4a494931 4121110907
    3649494936 464949291e 0000140000 0040340000 0008142241 1414141414
    0041221408 0201590906 3e415d594e 7c1211127c 7f49494936 3e41414122
    7f4141413e 7f49494941 7f09090901 3e41415173 7f0808087f 00417f4100
    2040413f01 7f08142241 7f40404040 7f021c027f 7f0408107f 3e4141413e
    7f09090906 3e4151215e 7f09192946 2649494932 03017f0103 3f4040403f
    1f2040201f 3f4038403f 6314081463 0304780403 6159494d43 007f414141
    0204081020 004141417f 0402010204 4040404040 0003070800 2054547840
    7f28444438 3844444428 384444287f 3854545418 00087e0902 18a4a49c78
    7f08040478 00447d4000 2040403d00 7f10284400 00417f4000 7c04780478
    7c08040478 3844444438 fc18242418 18242418fc 7c08040408 4854545424
    04043f4424 3c4040207c 1c2040201c 3c4030403c 4428102844 4c9090907c
    4464544c44 0008364100 0000770000 0041360800 0201020402
    """.split()
]
if len(FONT5X7) != 95:
    raise AssertionError("font table must contain 95 glyphs")


# ---------------------------------------------------------------------------
# Logging and hexdump helpers
# ---------------------------------------------------------------------------

def timestamp():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexs(data):
    """Compact uppercase hex string: '1B 7B 05 51 05 00 56 7D'."""
    return " ".join(f"{b:02X}" for b in data)


def hexdump(data, indent="  "):
    """Classic offset / 16 bytes / ASCII gutter dump."""
    data = bytes(data)
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexpart = " ".join(f"{b:02X}" for b in chunk)
        text = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        print(f"{indent}{off:08X}  {hexpart:<47}  |{text}|", flush=True)


class Logger:
    """Console output; -v enables every TX/RX hexdump.

    `emit` redirects info/debug lines (e.g. to the `logging` module when the
    tool is used as a library); hexdumps always go to stdout.
    """

    def __init__(self, verbose=False, emit=None):
        self.verbose = bool(verbose)
        self.emit = emit or (lambda message: print(message, flush=True))

    def info(self, message=""):
        self.emit(message)

    def debug(self, message):
        if self.verbose:
            self.emit(message)

    def tx(self, data, label=""):
        if not self.verbose:
            return
        suffix = f"  {label}" if label else ""
        print(f"{timestamp()}  --> TX {len(data)} B{suffix}", flush=True)
        hexdump(data)

    def rx(self, data, label=""):
        if not self.verbose:
            return
        suffix = f"  {label}" if label else ""
        print(f"{timestamp()}  <-- RX {len(data)} B{suffix}", flush=True)
        hexdump(data)


# ---------------------------------------------------------------------------
# Frame grammar and control frames (docs/PROTOCOL.md 1)
# ---------------------------------------------------------------------------

def frame_checksum(cmd, payload=b""):
    """ck = (cmd + sum(payload)) & 0xFF."""
    return (cmd + sum(payload)) & 0xFF


def make_frame(cmd, payload=b""):
    """Build 1B 7B <len> <cmd> <payload> <ck> 7D (len = payload + 3)."""
    payload = bytes(payload)
    if len(payload) > 252:
        raise ValueError("frame payload too large")
    return (bytes((ESC, LBRACE, len(payload) + 3, cmd)) + payload
            + bytes((frame_checksum(cmd, payload), RBRACE)))


def status_request_off():
    """Q(00) -- status request / status off: 1B 7B 05 51 00 00 51 7D."""
    return make_frame(CMD_STATUS, b"\x00\x00")


def status_request_on():
    """Q(05) -- status on: 1B 7B 05 51 05 00 56 7D."""
    return make_frame(CMD_STATUS, b"\x05\x00")


def printer_reset():
    """'!' reset: 1B 7B 03 21 21 7D."""
    return make_frame(CMD_RESET)


def print_end():
    """'@' print end / job start: 1B 7B 03 40 40 7D."""
    return make_frame(CMD_JOB_START)


def tape_feed():
    """Feed only: 1B 7B 04 2B 00 2B 7D."""
    return make_frame(CMD_FEED, b"\x00")


def tape_feed_cut():
    """Feed + cut: 1B 7B 04 2B 01 2C 7D."""
    return make_frame(CMD_FEED, b"\x01")


# ---------------------------------------------------------------------------
# Geometry (docs/PROTOCOL.md 5.4)
# ---------------------------------------------------------------------------

def mm_to_dots(mm):
    """180 dpi, 1 mm = 7.08661 dots; truncation matches the app/native code."""
    return int(float(mm) * DPI / MM_PER_INCH)


def printable_dots(tape_mm):
    try:
        return TAPE_DOTS[int(tape_mm)]
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"unsupported tape width {tape_mm} mm "
                         f"(LW-C410: {', '.join(str(t) for t in TAPE_CHOICES)})")


def bytes_per_row(dots):
    return (int(dots) + 7) // 8


# ---------------------------------------------------------------------------
# Job and page environments (docs/PROTOCOL.md 5.1, 5.2)
# ---------------------------------------------------------------------------

def job_environment_frames(density=0, cut="each"):
    """Return [(label, frame), ...] for the 39-byte job environment."""
    if cut not in CUT_VECTORS:
        raise ValueError(f"unknown cut mode {cut!r}")
    dparam = density + 5 if -5 <= density <= 5 else 0
    return [
        ("'@' job start", make_frame(CMD_JOB_START)),
        ("'{' job init signature", make_frame(CMD_JOB_INIT, JOB_INIT_SIGNATURE)),
        (f"'C' cutter ({CUT_LABELS[cut]})", make_frame(CMD_CUTTER, CUT_VECTORS[cut])),
        (f"'D' density {density:+d} (param {dparam})",
         make_frame(CMD_DENSITY, bytes((dparam,)))),
        ("'G' job env trailer", make_frame(CMD_JOB_TRAILER)),
    ]


def job_environment(density=0, cut="each"):
    return b"".join(frame for _, frame in job_environment_frames(density, cut))


def page_environment_frames(length_rows, margin_dots):
    """Return [(label, frame), ...] for the 18-byte command-level-1 page env."""
    length_rows = int(length_rows)
    margin_dots = int(margin_dots)
    if not 0 <= length_rows <= 0xFFFFFFFF:
        raise ValueError("page length does not fit in u32")
    if not 0 <= margin_dots <= 0xFFFF:
        raise ValueError("margin does not fit in u16")
    return [
        (f"'L' page length {length_rows} rows",
         make_frame(CMD_PAGE_LENGTH, length_rows.to_bytes(4, "little"))),
        (f"'T' margin {margin_dots} dots",
         make_frame(CMD_MARGIN, margin_dots.to_bytes(2, "little"))),
    ]


def page_environment(length_rows, margin_dots):
    return b"".join(frame for _, frame in page_environment_frames(length_rows, margin_dots))


# ---------------------------------------------------------------------------
# Status frame (docs/PROTOCOL.md 4)
# ---------------------------------------------------------------------------

def tw_text(code):
    width = TW_TAPE_MM.get(code)
    if width is None:
        return "unknown"
    if width == 0:
        return "no tape"
    return f"{width} mm"


def tr_text(code):
    if code in TR_TEXT:
        return TR_TEXT[code]
    if 0x54 <= code <= 0x58:
        return f"die-cut {code - 0x53}"
    if 0x60 <= code <= 0x6B:
        return "die-cut shape"
    if 0x91 <= code <= 0x98:
        return "big roll"
    if 0x90 <= code <= 0x9F:
        return "wide (reserved)"
    return "unknown"


class StatusFrame:
    """One 64-byte '@' ... 0xFF status frame with decoded key/value fields."""

    def __init__(self, raw, fields):
        self.raw = bytes(raw)
        self.fields = dict(fields)

    @classmethod
    def parse(cls, frame):
        """Parse exactly 64 bytes; returns None if the delimiters are wrong."""
        frame = bytes(frame)
        if len(frame) != STATUS_FRAME_LEN or frame[0] != 0x40 or frame[63] != 0xFF:
            return None
        body = frame[1:63].decode("latin-1")
        fields = {}
        for match in KEY_VALUE_RE.finditer(body):
            # Last occurrence wins, mirroring the app's lastIndexOf(KEY)+3..+5.
            fields[match.group(1).upper()] = int(match.group(2), 16)
        return cls(frame, fields)

    # -- raw fields ---------------------------------------------------------
    @property
    def st(self):
        return self.fields.get("ST", 0xFF)

    @property
    def er(self):
        if "ER" in self.fields:
            return self.fields["ER"]
        return self.fields.get("ER2", 0x00)

    @property
    def tw(self):
        return self.fields.get("TW", 0x00)

    @property
    def tr(self):
        return self.fields.get("TR", 0x00)

    # -- decoded fields -----------------------------------------------------
    @property
    def st_text(self):
        return ST_TEXT.get(self.st, "unknown")

    @property
    def er_text(self):
        return ER_TEXT.get(self.er, "unknown error")

    @property
    def tape_width_mm(self):
        """Width in mm; 0 = no tape, -1 = unknown TW code."""
        if self.tw == 0x00:
            return 0
        return TW_TAPE_MM.get(self.tw, -1)

    @property
    def is_error(self):
        return self.er != 0x00 or self.st == 0xFF

    @property
    def is_ready(self):
        return self.er == 0x00 and self.st in READY_STATES

    def field(self, key, default=None):
        return self.fields.get(key, default)

    def summary(self):
        return (f"ST={self.st:02X} ({self.st_text}) "
                f"ER={self.er:02X} ({self.er_text}) "
                f"TW={self.tw:02X} ({tw_text(self.tw)}) "
                f"TR={self.tr:02X} ({tr_text(self.tr)})")


class StatusParser:
    """Streaming parser with app-style backward resync.

    Accumulates bytes and, when it sees 0xFF with 0x40 exactly 63 bytes before
    it, extracts the 64-byte frame. The scan runs backwards from the newest
    byte, so it skips leading garbage and older stale frames (PROTOCOL.md 4.1).
    """

    MAX_BUFFER = 4096

    def __init__(self):
        self.buf = bytearray()

    def reset(self):
        self.buf.clear()

    def feed(self, data):
        """Append received bytes; return a list of complete StatusFrames."""
        self.buf.extend(data)
        frames = []
        while True:
            frame = self._extract()
            if frame is None:
                break
            frames.append(frame)
        if len(self.buf) > self.MAX_BUFFER:
            # Nothing frame-like for a long time: keep only the tail.
            del self.buf[:len(self.buf) - 256]
        return frames

    def _extract(self):
        buf = self.buf
        for end in range(len(buf) - 1, STATUS_FRAME_LEN - 2, -1):
            if buf[end] == 0xFF and buf[end - 63] == 0x40:
                frame = StatusFrame.parse(buf[end - 63:end + 1])
                if frame is not None:
                    del buf[:end + 1]
                    return frame
        return None


def describe_status_key(key, code):
    if key == "ST":
        return ST_TEXT.get(code, "unknown")
    if key in ("ER", "ER2"):
        return ER_TEXT.get(code, "unknown error")
    if key == "TW":
        return tw_text(code)
    if key == "TR":
        return tr_text(code)
    return ""


def print_status_frame(status, title=""):
    """Pretty-print one frame: every documented field plus the raw bytes."""
    if title:
        print(title)
    print(f"  raw: {hexs(status.raw)}")
    for key in STATUS_KEYS:
        code = status.fields.get(key)
        if key == "ER" and code is None:
            code = status.fields.get("ER2")
        if code is None:
            print(f"  {key:<2}   --")
            continue
        meaning = describe_status_key(key, code)
        print(f"  {key:<2}   {code:02X}" + (f"   {meaning}" if meaning else ""))
    extra = [k for k in sorted(status.fields)
             if k not in STATUS_KEYS and k != "ER2"]
    if extra:
        print("  also: " + ", ".join(f"{k}={status.fields[k]:02X}" for k in extra))


# ---------------------------------------------------------------------------
# Bitmap, patterns and image input
# ---------------------------------------------------------------------------

class Bitmap:
    """1-bit label bitmap in printer orientation.

    rows = position along the tape (raster row index), cols = position across
    the tape (raster bit index), value 1 = printed dot.
    """

    def __init__(self, rows, cols, px=None):
        self.rows = int(rows)
        self.cols = int(cols)
        if self.rows <= 0 or self.cols <= 0:
            raise ValueError("bitmap dimensions must be positive")
        if px is None:
            self.px = [bytearray(self.cols) for _ in range(self.rows)]
        else:
            if len(px) != self.rows:
                raise ValueError("bitmap row count mismatch")
            self.px = [bytearray(row) for row in px]

    def set(self, v, u, value=1):
        if 0 <= v < self.rows and 0 <= u < self.cols:
            self.px[v][u] = value


def resize_bitmap(bitmap, rows=None, cols=None):
    """Nearest-neighbour resize (the tape dimension is scaled to fit the head)."""
    out_rows = bitmap.rows if rows is None else int(rows)
    out_cols = bitmap.cols if cols is None else int(cols)
    if out_rows <= 0 or out_cols <= 0:
        raise ValueError("scaled bitmap would be empty")
    if out_rows == bitmap.rows and out_cols == bitmap.cols:
        return bitmap
    out = Bitmap(out_rows, out_cols)
    for v in range(out_rows):
        src = bitmap.px[min(bitmap.rows - 1, v * bitmap.rows // out_rows)]
        dst = out.px[v]
        for u in range(out_cols):
            dst[u] = src[min(bitmap.cols - 1, u * bitmap.cols // out_cols)]
    return out


def _pbm_header(data):
    """Parse P1/P4 magic + dimensions; return (magic, width, height, offset)."""
    i = 0
    tokens = []
    while len(tokens) < 3:
        while i < len(data) and data[i] in b" \t\r\n\v\f":
            i += 1
        if i < len(data) and data[i] == 0x23:      # '#" comment to end of line
            end = data.find(b"\n", i)
            if end < 0:
                raise ValueError("invalid PBM header (unterminated comment)")
            i = end + 1
            continue
        start = i
        while i < len(data) and data[i] not in b" \t\r\n\v\f":
            i += 1
        if i == start:
            raise ValueError("invalid PBM header")
        tokens.append(data[start:i])
    if i < len(data) and data[i] in b" \t\r\n\v\f":
        i += 1                                     # single whitespace before raster
    try:
        width = int(tokens[1])
        height = int(tokens[2])
    except ValueError:
        raise ValueError("invalid PBM dimensions")
    return tokens[0], width, height, i


def load_pbm(data, invert=False):
    """Parse PBM P4 (binary, 1=black) or P1 (ASCII, 1=black) into a Bitmap.

    PBM width becomes the along-tape axis and height the across-tape axis.
    """
    magic, width, height, offset = _pbm_header(data)
    if width <= 0 or height <= 0:
        raise ValueError("empty PBM image")
    px = [bytearray(height) for _ in range(width)]

    if magic == b"P4":
        row_bytes = (width + 7) // 8
        if offset + row_bytes * height > len(data):
            raise ValueError("truncated P4 raster data")
        for y in range(height):
            base = offset + y * row_bytes
            for x in range(width):
                bit = (data[base + (x >> 3)] >> (7 - (x & 7))) & 1
                px[x][y] = 0 if bit else 1 if invert else 0
                if invert and not bit:
                    px[x][y] = 1
    elif magic == b"P1":
        values = []
        for token in data[offset:].split():
            token = token.split(b"#", 1)[0]
            if token:
                values.append(token)
        if len(values) < width * height:
            raise ValueError("truncated P1 raster data")
        for y in range(height):
            for x in range(width):
                bit = 1 if int(values[y * width + x]) else 0
                printed = (not bit) if invert else bool(bit)
                px[x][y] = 1 if printed else 0
    else:
        raise ValueError(f"unsupported PBM magic {magic!r}")

    return Bitmap(width, height, px)


def load_png(path, invert=False, threshold=140):
    """PNG input via optional Pillow; luma < threshold means printed."""
    try:
        from PIL import Image
    except ImportError:
        raise ValueError(
            "PNG input needs Pillow (pip install pillow); alternatively convert "
            "the image to PBM (P4), e.g. ImageMagick: convert in.png -threshold 55% out.pbm")
    try:
        image = Image.open(path).convert("L")
    except Exception as exc:  # PIL raises various types
        raise ValueError(f"cannot read PNG {path}: {exc}")
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("empty PNG image")
    pixels = image.load()
    px = [bytearray(height) for _ in range(width)]
    for y in range(height):
        for x in range(width):
            luma = pixels[x, y]
            printed = luma >= threshold if invert else luma < threshold
            px[x][y] = 1 if printed else 0
    return Bitmap(width, height, px)


def load_image(path, invert=False, threshold=140):
    """Load PBM P1/P4 (stdlib) or PNG (Pillow, if installed)."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ValueError(f"cannot read image {path}: {exc}")
    if data[:8] == PNG_MAGIC or path.lower().endswith(".png"):
        return load_png(path, invert=invert, threshold=threshold)
    if data[:2] in (b"P1", b"P4"):
        return load_pbm(data, invert=invert)
    raise ValueError(f"unsupported image format in {path}; use PBM (P1/P4) or PNG")


def text_bitmap(text, dots, length_mm=None):
    """Render text with the built-in 5x7 font, centred across and along the tape."""
    text = "".join(ch for ch in text.upper() if 0x20 <= ord(ch) <= 0x7E)
    if not text:
        text = " "
    # Largest integer scale that fits the tape height (7 rows + 2 dots padding).
    scale = 1
    while 7 * (scale + 1) <= dots - 2:
        scale += 1
    glyph_w = 5 * scale
    gap = scale
    text_w = len(text) * glyph_w + (len(text) - 1) * gap
    pad = 2 * scale
    total_rows = mm_to_dots(length_mm) if length_mm is not None else text_w + 2 * pad
    total_rows = max(total_rows, 1)
    bitmap = Bitmap(total_rows, dots)
    x0 = max(0, (total_rows - text_w) // 2)
    y0 = max(0, (dots - 7 * scale) // 2)
    for index, ch in enumerate(text):
        glyph = FONT5X7[ord(ch) - 0x20]
        gx0 = x0 + index * (glyph_w + gap)
        for col in range(5):
            bits = glyph[col]
            for row in range(7):
                if bits & (1 << row):
                    for sy in range(scale):
                        for sx in range(scale):
                            bitmap.set(gx0 + col * scale + sx, y0 + row * scale + sy, 1)
    return bitmap


def _corner_marker(bitmap):
    """Asymmetric 6x6 block at the top-right (label space) to reveal mirroring."""
    for v in range(min(6, bitmap.rows)):
        for u in range(max(0, bitmap.cols - 6), bitmap.cols):
            bitmap.px[v][u] = 1


def make_pattern(pattern, tape_mm, length_mm=None):
    """Build a test pattern: lines | checker | bars | solid | text <TEXT>."""
    dots = printable_dots(tape_mm)
    name = pattern.strip()
    lowered = name.lower()

    if lowered == "text" or lowered.startswith("text ") or lowered.startswith("text:"):
        text = name[4:].lstrip(" :")
        return text_bitmap(text or "HELLO", dots, length_mm=length_mm)

    if length_mm is not None:
        rows = mm_to_dots(length_mm)
    else:
        rows = mm_to_dots(25.0)          # worked-example default (PROTOCOL.md 5.9)
    rows = max(1, rows)
    bitmap = Bitmap(rows, dots)

    if lowered == "lines":
        # 2-dot lines across the tape every 8 dots along it.
        for v in range(rows):
            if v % 8 < 2:
                bitmap.px[v] = bytearray(b"\x01" * dots)
    elif lowered == "checker":
        for v in range(rows):
            row = bitmap.px[v]
            for u in range(dots):
                row[u] = ((v >> 3) ^ (u >> 3)) & 1
    elif lowered == "bars":
        # 4-dot stripes across the tape along the whole label.
        for v in range(rows):
            row = bitmap.px[v]
            for u in range(dots):
                row[u] = 1 if (u // 4) % 2 == 0 else 0
    elif lowered == "solid":
        for v in range(rows):
            bitmap.px[v] = bytearray(b"\x01" * dots)
    else:
        raise ValueError(f"unknown pattern {pattern!r} "
                         "(use lines, checker, bars, solid or 'text HELLO')")

    _corner_marker(bitmap)
    return bitmap


# ---------------------------------------------------------------------------
# Raster packing and job plan (docs/PROTOCOL.md 5.3, 5.5)
# ---------------------------------------------------------------------------

def raster_header(dots):
    """1B 2E 00 00 00 01 <dots u16le>."""
    if not 0 <= dots <= 0xFFFF:
        raise ValueError("dot count does not fit in u16")
    return RASTER_HEADER_PREFIX + int(dots).to_bytes(2, "little")


def raster_row_bytes(bitmap, row, mirror=False):
    """Pack one raster row, MSB first, bit set = printed dot."""
    out = bytearray(bytes_per_row(bitmap.cols))
    src = bitmap.px[row]
    dots = bitmap.cols
    for u in range(dots):
        source_col = dots - 1 - u if mirror else u
        if src[source_col]:
            out[u >> 3] |= 0x80 >> (u & 7)
    return bytes(out)


class PagePlan:
    def __init__(self, page_env, page_env_frames, bands, band_rows, content_rows):
        self.page_env = page_env
        self.page_env_frames = page_env_frames
        self.bands = bands
        self.band_rows = band_rows
        self.content_rows = content_rows


class JobPlan:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def stream_bytes(self):
        """Full print stream (control frames Q(05)/Q(00) are not included)."""
        data = bytearray(self.job_env)
        for page in self.pages:
            data += page.page_env
            for band in page.bands:
                data += band
            data += END_OF_PAGE
        data += self.job_end
        return bytes(data)


def build_job_plan(bitmap, tape_mm, margin_mm=DEFAULT_MARGIN_MM, density=0, cut="each",
                   copies=1, band_rows=MAX_BAND_ROWS, mirror=False,
                   band_delay_ms=BAND_DELAY_MS):
    """Turn a Bitmap into the complete print stream, banded for the printer."""
    dots = printable_dots(tape_mm)
    if bitmap.cols != dots:
        raise ValueError(f"bitmap has {bitmap.cols} dots across, tape needs {dots}")
    if bitmap.rows < 1:
        raise ValueError("label has no rows")
    band_rows = int(band_rows)
    if band_rows < 1:
        raise ValueError("--band-rows must be >= 1")
    if band_rows > MAX_BAND_ROWS:
        band_rows = MAX_BAND_ROWS
    margin_dots = mm_to_dots(margin_mm)
    content_rows = bitmap.rows
    length_rows = 2 * margin_dots + content_rows     # PROTOCOL.md 5.2: L = 2*T + rows

    header = raster_header(dots)
    blank_row = bytes(header + bytes(bytes_per_row(dots)))
    content_packed = [header + raster_row_bytes(bitmap, v, mirror=mirror)
                      for v in range(content_rows)]
    # The app sends L rows per page: T blank + content + T blank (PROTOCOL.md
    # 5.2/5.9 - "raster x L rows"). Sending only content rows makes the printer
    # align the received rows to the page END and cut right after them: the
    # cutter then clips the last content rows (observed on hardware: first
    # glyph cut in half) and the missing 2*T rows feed as blank in front of
    # the content (observed as ~2*T dots of leading waste).
    packed_rows = ([blank_row] * margin_dots + content_packed +
                   [blank_row] * margin_dots)

    bands = []
    band_sizes = []
    for start in range(0, len(packed_rows), band_rows):
        chunk = packed_rows[start:start + band_rows]
        bands.append(b"".join(chunk))
        band_sizes.append(len(chunk))

    page_frames = page_environment_frames(length_rows, margin_dots)
    page = PagePlan(b"".join(fr for _, fr in page_frames), page_frames,
                    bands, band_sizes, content_rows)

    job_frames = job_environment_frames(density, cut)
    return JobPlan(
        tape_mm=int(tape_mm),
        dots=dots,
        row_bytes=bytes_per_row(dots),
        margin_mm=float(margin_mm),
        margin_dots=margin_dots,
        content_rows=content_rows,
        length_rows=length_rows,
        density=int(density),
        cut=cut,
        copies=int(copies),
        mirror=bool(mirror),
        band_rows=band_rows,
        band_delay_ms=int(band_delay_ms),
        job_env=b"".join(fr for _, fr in job_frames),
        job_env_frames=job_frames,
        pages=[page for _ in range(max(1, int(copies)))],
        job_end=print_end(),
        raster_header=header,
    )


def report_plan(plan, logger):
    """Dry-run / pre-print summary of the byte stream and its statistics."""
    print(f"Tape: {plan.tape_mm} mm -> {plan.dots} dots across, "
          f"{plan.row_bytes} bytes/row")
    print(f"Content: {plan.content_rows} rows along the tape; margin "
          f"{plan.margin_mm:g} mm = {plan.margin_dots} dots")
    print(f"Page length L = 2*{plan.margin_dots} + {plan.content_rows} = "
          f"{plan.length_rows} rows (0x{plan.length_rows:X})")
    print(f"Copies: {plan.copies}; cut: {CUT_LABELS[plan.cut]}; "
          f"density: {plan.density:+d}; mirror: {'yes' if plan.mirror else 'no'}; "
          f"bands: <= {plan.band_rows} rows, {plan.band_delay_ms} ms pacing")

    print(f"Job environment ({len(plan.job_env)} bytes):")
    for label, frame in plan.job_env_frames:
        print(f"  {hexs(frame):<36} {label}")

    page = plan.pages[0]
    print(f"Page environment ({len(page.page_env)} bytes):")
    for label, frame in page.page_env_frames:
        print(f"  {hexs(frame):<36} {label}")

    print(f"Raster header: {hexs(plan.raster_header)}")
    for index, (band, rows) in enumerate(zip(page.bands, page.band_rows), 1):
        print(f"  band {index}/{len(page.bands)}: {rows} rows, {len(band)} bytes")
    if page.bands:
        print(f"  first raster row: {hexs(page.bands[0][:8 + plan.row_bytes])}")
    print(f"End of page: {hexs(END_OF_PAGE)}    End of job: {hexs(plan.job_end)}")
    print(f"Print stream: {len(plan.stream_bytes())} bytes "
          f"({plan.copies} copy/copies)")
    print("Live sessions also send Q(05) after the job env and Q(00) at the end "
          "(control frames, not part of the stream above).")
    if logger.verbose:
        print("Full print stream:")
        hexdump(plan.stream_bytes())


# ---------------------------------------------------------------------------
# Transport: shared stream base, Bluetooth Classic SPP / RFCOMM (Linux),
# POSIX serial port (macOS / USB bridges)
# ---------------------------------------------------------------------------

class TransportError(RuntimeError):
    pass


class ProtocolError(RuntimeError):
    pass


class PrinterUnavailableError(TransportError):
    """Connecting or session setup failed before any job data was sent:
    printer off / out of range / wedged / cover open. Safe to retry."""


class TapeMismatchError(ProtocolError):
    """The loaded cassette does not match the job (not worth retrying)."""


def normalize_mac(mac):
    mac = (mac or "").strip()
    if not MAC_RE.match(mac):
        raise ValueError(f"invalid Bluetooth address {mac!r} "
                         "(expected AA:BB:CC:DD:EE:FF; use `scan` to find it)")
    return mac.upper()


def discover_spp_channel(mac, timeout=6.0, logger=None):
    """Return the SPP (UUID 1101) RFCOMM channel via sdptool, or None."""
    exe = shutil.which("sdptool")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "browse", "--bdaddr", mac],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        if logger:
            logger.debug(f"sdptool failed: {exc}")
        return None
    text = proc.stdout or ""
    # Prefer the Serial Port / SPP record.
    for block in re.split(r"\n\s*\n", text):
        if re.search(r"0x1101|Serial Port|SPP", block, re.IGNORECASE):
            match = re.search(r"Channel:\s*(\d+)", block)
            if match:
                return int(match.group(1))
    match = re.search(r"Channel:\s*(\d+)", text)
    return int(match.group(1)) if match else None


class StreamTransport:
    """Common byte-stream transport: status parsing, pending frames, timeouts.

    Subclasses implement only `connect()`, `close()`, `_send_bytes()` and
    `_recv_chunk()`. Everything else -- the streaming StatusParser, the queue of
    already parsed frames, the verbose TX/RX hexdumps and the deadline handling
    -- is identical for RFCOMM and for a POSIX serial port.
    """

    def __init__(self, logger=None, connect_timeout=CONNECT_TIMEOUT,
                 read_timeout=READ_TIMEOUT, write_timeout=WRITE_TIMEOUT,
                 band_write_timeout=BAND_WRITE_TIMEOUT):
        self.log = logger or Logger(False)
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.write_timeout = float(write_timeout)
        self.band_write_timeout = float(band_write_timeout)
        self.parser = StatusParser()
        self.pending = []

    # -- connection (subclass hooks) ----------------------------------------
    def connect(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def _send_bytes(self, data, timeout):
        """Write all of `data`, honouring `timeout` seconds."""
        raise NotImplementedError

    def _recv_chunk(self, timeout):
        """Return the next chunk of bytes, or None if nothing arrived in time."""
        raise NotImplementedError

    def _reset_parser(self):
        self.parser.reset()
        self.pending = []

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- io -----------------------------------------------------------------
    def write(self, data, label="", timeout=None):
        data = bytes(data)
        self.log.tx(data, label)
        try:
            self._send_bytes(data, self.write_timeout if timeout is None
                             else float(timeout))
        except OSError as exc:
            raise TransportError(f"write failed: {exc}")

    def _recv(self, timeout):
        """One chunk from the backend, hexdumped; None on timeout."""
        chunk = self._recv_chunk(timeout)
        if chunk:
            self.log.rx(chunk)
        return chunk

    def read_status(self, timeout=None):
        """Return the next StatusFrame or None if none arrived within timeout."""
        if self.pending:
            return self.pending.pop(0)
        deadline = time.monotonic() + (self.read_timeout if timeout is None else float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            chunk = self._recv(min(remaining, self.read_timeout))
            if chunk is None:
                continue
            self.pending.extend(self.parser.feed(chunk))
            if self.pending:
                return self.pending.pop(0)

    def poll_frames(self, timeout):
        """Read one chunk; return any complete frames (possibly an empty list)."""
        if self.pending:
            frames = self.pending
            self.pending = []
            return frames
        chunk = self._recv(timeout)
        if chunk is None:
            return []
        return self.parser.feed(chunk)

    def poll(self, timeout):
        """One read cycle: (frames, raw_chunk). raw_chunk is what was read in
        this cycle (None on timeout) -- lets debug commands show traffic that
        did not form a complete status frame instead of dropping it silently."""
        if self.pending:
            frames = self.pending
            self.pending = []
            return frames, None
        chunk = self._recv(timeout)
        if chunk is None:
            return [], None
        return self.parser.feed(chunk), chunk


class RfcommTransport(StreamTransport):
    """RFCOMM socket backend (Linux BlueZ) with status parsing and app-style retries."""

    def __init__(self, mac, channel=None, logger=None, connect_timeout=CONNECT_TIMEOUT,
                 read_timeout=READ_TIMEOUT, write_timeout=WRITE_TIMEOUT,
                 band_write_timeout=BAND_WRITE_TIMEOUT, retry=True):
        super().__init__(logger=logger, connect_timeout=connect_timeout,
                         read_timeout=read_timeout, write_timeout=write_timeout,
                         band_write_timeout=band_write_timeout)
        self.mac = normalize_mac(mac)
        self.channel = channel
        self.retry = bool(retry)
        self.sock = None

    # -- connection ---------------------------------------------------------
    def _open_socket(self, channel):
        if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_RFCOMM"):
            raise TransportError(
                "this Python socket module has no AF_BLUETOOTH/BTPROTO_RFCOMM support; "
                "Bluetooth Classic requires Linux with BlueZ")
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                             socket.BTPROTO_RFCOMM)
        sock.settimeout(self.connect_timeout)
        try:
            sock.connect((self.mac, int(channel)))
        except OSError:
            sock.close()
            raise
        sock.settimeout(self.read_timeout)
        return sock

    def _channel_sequence(self):
        if self.channel is not None:
            seq = [int(self.channel)]
            return seq * (4 if self.retry else 1)
        sdp = discover_spp_channel(self.mac, logger=self.log)
        if sdp:
            self.log.info(f"SDP: SPP service on RFCOMM channel {sdp}")
        elif shutil.which("sdptool"):
            self.log.info("SDP: no SPP channel found via sdptool")
        else:
            self.log.info("SDP: sdptool not available")
        # App order: 2x SDP/UUID channel, then 2x channel 1. The LW-C410's
        # SPP actually lives on server channel 6 (HCI snoop, PROTOCOL.md 2),
        # so without an SDP result we try 6 before the legacy channel-1
        # fallback.
        first = sdp if sdp else SPP_CHANNEL_SNOOP
        if not self.retry:
            return [first]
        return [first, first, 1, 1]

    def connect(self):
        if self.sock is not None:
            return
        sequence = self._channel_sequence()
        errors = []
        last_open = None
        for attempt, channel in enumerate(sequence, 1):
            if last_open is not None:
                delay = CONNECT_RETRY_DELAY - (time.monotonic() - last_open)
                if delay > 0:
                    time.sleep(delay)
            self.log.info(f"Connecting {self.mac} on RFCOMM channel {channel} "
                          f"(attempt {attempt}/{len(sequence)}) ...")
            last_open = time.monotonic()
            try:
                sock = self._open_socket(channel)
            except OSError as exc:
                errors.append(f"channel {channel}: {exc}")
                self.log.info(f"  failed: {exc}")
                continue
            self.sock = sock
            self.channel = channel
            self._reset_parser()
            self.log.info(f"Connected (RFCOMM channel {channel}).")
            return
        raise TransportError("could not connect: " + "; ".join(errors))

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -- io -----------------------------------------------------------------
    def _send_bytes(self, data, timeout):
        if self.sock is None:
            raise TransportError("not connected")
        self.sock.settimeout(float(timeout))
        self.sock.sendall(data)

    def _recv_chunk(self, timeout):
        if self.sock is None:
            raise TransportError("not connected")
        self.sock.settimeout(max(0.001, float(timeout)))
        try:
            chunk = self.sock.recv(1024)
        except socket.timeout:
            return None
        except OSError as exc:
            raise TransportError(f"read failed: {exc}")
        if not chunk:
            raise TransportError("connection closed by the printer")
        return chunk


class SerialPortTransport(StreamTransport):
    """POSIX serial-port backend: macOS /dev/cu.*, Linux /dev/tty*, /dev/pts/*.

    On macOS a paired Bluetooth Classic device is exposed by the OS as a serial
    port, which is the only stdlib way to reach SPP there. The line settings are
    forced to raw 8N1; the baud rate is deliberately left alone because
    Bluetooth serial ports ignore it. A non-tty fd (plain pty, pipe) is accepted
    -- termios config is best effort.
    """

    def __init__(self, path, logger=None, connect_timeout=CONNECT_TIMEOUT,
                 read_timeout=READ_TIMEOUT, write_timeout=WRITE_TIMEOUT,
                 band_write_timeout=BAND_WRITE_TIMEOUT, settle=LINK_SETTLE):
        super().__init__(logger=logger, connect_timeout=connect_timeout,
                         read_timeout=read_timeout, write_timeout=write_timeout,
                         band_write_timeout=band_write_timeout)
        self.path = str(path)
        self.fd = None
        self.settle = max(0.0, float(settle))

    # -- connection ---------------------------------------------------------
    def _open_port(self):
        # BLOCKING open (like pyserial): on macOS a Bluetooth serial port
        # only initiates the outgoing RFCOMM connection on a blocking open;
        # opening O_NONBLOCK can leave the link unestablished and writes are
        # then silently discarded. A SIGALRM watchdog bounds the wait so a
        # missing device cannot hang us forever (main thread only).
        def _alarm(signum, frame):
            raise OSError(errno.ETIMEDOUT,
                          f"open({self.path}) timed out after "
                          f"{self.connect_timeout:g} s (is the printer on?")
        watchdog = None
        try:
            watchdog = signal.signal(signal.SIGALRM, _alarm)
            signal.setitimer(signal.ITIMER_REAL, self.connect_timeout)
        except (ValueError, OSError):
            watchdog = None      # not the main thread: plain blocking open
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY)
        finally:
            if watchdog is not None:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, watchdog)
        # switch to non-blocking for the select()-driven I/O loops
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)
        except OSError:
            pass
        try:
            self._configure(fd)
        except OSError:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _configure(fd):
        """Raw 8N1; silently keep the fd as-is if it is not a terminal."""
        if termios is None:
            return
        try:
            attrs = termios.tcgetattr(fd)
        except (termios.error, OSError):
            return                      # ENOTTY: pty/pipe/regular file
        iflag, oflag, cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
                   termios.ISTRIP | termios.INLCR | termios.IGNCR |
                   termios.ICRNL | termios.IXON)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
                   termios.ISIG | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[0], attrs[1], attrs[2], attrs[3] = iflag, oflag, cflag, lflag
        try:
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except (termios.error, OSError):
            pass

    def connect(self):
        if self.fd is not None:
            return
        self.log.info(f"Opening serial port {self.path} ...")
        errors = []
        for attempt in (1, 2):
            if attempt == 2:
                time.sleep(CONNECT_RETRY_DELAY)
            try:
                fd = self._open_port()
            except OSError as exc:
                errors.append(str(exc))
                self.log.info(f"  attempt {attempt}/2 failed: {exc}")
                continue
            self.fd = fd
            self._reset_parser()
            if self.settle > 0:
                # macOS Bluetooth serial ports: the RFCOMM channel comes up
                # asynchronously after open(); early writes can be dropped.
                self.log.info(f"Connected (serial port {self.path}); settling "
                              f"{self.settle:g} s for the link to come up ...")
                time.sleep(self.settle)
            else:
                self.log.info(f"Connected (serial port {self.path}).")
            return
        raise TransportError(f"could not open serial port {self.path}: "
                             + "; ".join(errors))

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass                        # EBADF and friends: nothing to do
            self.fd = None

    # -- io -----------------------------------------------------------------
    def _send_bytes(self, data, timeout):
        if self.fd is None:
            raise TransportError("not connected")
        timeout = float(timeout)
        deadline = time.monotonic() + timeout
        view = memoryview(bytes(data))
        while len(view):
            try:
                written = os.write(self.fd, view)
            except BlockingIOError:
                written = 0
            except OSError as exc:
                if exc.errno != errno.EAGAIN:
                    raise
                written = 0
            if written:
                view = view[written:]
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError(f"write timed out after {timeout:g} s")
            try:
                _, writable, _ = select.select([], [self.fd], [], remaining)
            except (OSError, ValueError) as exc:
                raise TransportError(f"write failed: {exc}")
            if not writable:
                raise TransportError(f"write timed out after {timeout:g} s")

    def _recv_chunk(self, timeout):
        if self.fd is None:
            raise TransportError("not connected")
        timeout = max(0.0, float(timeout))
        try:
            readable, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError) as exc:
            raise TransportError(f"read failed: {exc}")
        if not readable:
            return None
        try:
            chunk = os.read(self.fd, 1024)
        except BlockingIOError:
            return None
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                return None
            raise TransportError(f"read failed: {exc}")
        if not chunk:
            raise TransportError("connection closed by the printer")
        return chunk


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

def _parse_bt_scan_output(text, devices):
    for raw_line in ANSI_RE.sub("", text).splitlines():
        line = raw_line.strip()
        match = re.match(r"^(?:\[NEW\]|\[CHG\])\s+Device\s+([0-9A-Fa-f:]{17})\s*(.*)$",
                         line)
        if match:
            mac = match.group(1).upper()
            rest = match.group(2).strip()
            name = ""
            if rest.startswith("Name:"):
                name = rest[5:].strip()
            elif rest and not re.match(
                    r"^(RSSI|TXPower|UUIDs|Class|Icon|Connected|Trusted|Blocked|"
                    r"Paired|LegacyPairing|Modalias|ManufacturerData|ServiceData|"
                    r"ServicesResolved|WakeAllowed|Alias|Battery):", rest):
                name = rest
            if name:
                devices[mac] = name
            else:
                devices.setdefault(mac, "")
            continue
        # hcitool scan: "  AA:BB:CC:DD:EE:FF\tname"
        match = re.match(r"^([0-9A-Fa-f:]{17})\s+(.+)$", line)
        if match:
            devices[match.group(1).upper()] = match.group(2).strip()


def _run_bluetoothctl_scan(seconds, logger):
    exe = shutil.which("bluetoothctl")
    if not exe:
        return None
    seconds = int(max(1, seconds))
    commands = [
        [exe, "--timeout", str(seconds), "scan", "on"],
        [exe, f"--timeout={seconds}", "scan", "on"],
    ]
    for command in commands:
        try:
            proc = subprocess.run(command, capture_output=True, text=True,
                                  timeout=seconds + 15)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"{' '.join(command[:2])} failed: {exc}")
            continue
        if proc.returncode == 0:
            return (proc.stdout or "") + (proc.stderr or "")
        logger.debug(f"bluetoothctl option form failed (rc={proc.returncode}): "
                     f"{' '.join(command[1:])}")
    # Older bluetoothctl: scan in the background and terminate after N seconds.
    try:
        proc = subprocess.Popen([exe, "scan", "on"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        time.sleep(max(1, seconds))
        proc.terminate()
        try:
            output, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
        return output or ""
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"bluetoothctl fallback scan failed: {exc}")
        return None


def scan_devices(seconds, logger):
    """Discover devices; returns {MAC: name}. bluetoothctl preferred, hcitool fallback."""
    devices = {}
    output = _run_bluetoothctl_scan(seconds, logger)
    if output is not None:
        logger.info(f"Scanning with bluetoothctl for {seconds:g} s ...")
        _parse_bt_scan_output(output, devices)
        return devices
    exe = shutil.which("hcitool")
    if exe:
        logger.info(f"bluetoothctl unavailable; scanning with hcitool (up to {seconds:g} s) ...")
        try:
            proc = subprocess.run([exe, "scan"], capture_output=True, text=True,
                                  timeout=seconds + 15)
            _parse_bt_scan_output((proc.stdout or "") + (proc.stderr or ""), devices)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TransportError(f"hcitool scan failed: {exc}")
        return devices
    raise TransportError("neither bluetoothctl nor hcitool found; install BlueZ")


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def session_prepare(transport, logger, expect_tape=None, status_timeout=10.0,
                    reset=True, strict_tape=False):
    """Run session-flow steps 2-5: Q(00), '!' reset, Q(05), read a ready status.

    Q(05) is re-sent up to STATUS_ATTEMPTS times: on serial links (macOS)
    bytes written right after open() can be lost while the Bluetooth link is
    still coming up, so a missing first reply is retried before giving up.
    """
    transport.write(status_request_off(), "Q(00) status off")
    status = transport.read_status(timeout=min(3.0, status_timeout))
    if status is not None:
        logger.debug("  after Q(00): " + status.summary())

    if reset:
        transport.write(printer_reset(), "! printer reset")

    window = max(1.0, min(STATUS_ATTEMPT_WINDOW, status_timeout / STATUS_ATTEMPTS))
    status = None
    for attempt in range(1, STATUS_ATTEMPTS + 1):
        transport.write(status_request_on(),
                        f"Q(05) status on (attempt {attempt}/{STATUS_ATTEMPTS})")
        status = transport.read_status(timeout=window)
        if status is not None:
            break
        if attempt < STATUS_ATTEMPTS:
            logger.info(f"no status frame within {window:g} s; re-sending Q(05) ...")
    if status is None:
        raise ProtocolError(
            f"no status frame received after {STATUS_ATTEMPTS}x Q(05) "
            f"(total {STATUS_ATTEMPTS * window:g} s). Re-run with -v to hexdump "
            "all traffic, and try `monitor --no-send` (does the printer talk at "
            "all?) or `raw --repeat 3 1B 7B 05 51 05 00 56 7D` (is it hearing us?)")

    deadline = time.monotonic() + status_timeout
    while True:
        logger.info("printer: " + status.summary())
        if status.er != 0x00:
            raise ProtocolError(f"printer error {status.er:02X}: {status.er_text}")
        if status.st in READY_STATES:
            break
        if status.st in BUSY_STATES:
            time.sleep(0.05)     # printer is finishing something; it keeps pushing
            status = transport.read_status(
                timeout=max(0.2, deadline - time.monotonic()))
            if status is None:
                raise ProtocolError("printer stayed busy; no further status frames")
            continue
        raise ProtocolError(f"printer not ready: ST={status.st:02X} {status.st_text}")

    if expect_tape is not None:
        width = status.tape_width_mm
        if width not in (0, -1) and width != expect_tape:
            message = (f"printer reports {width} mm tape, "
                       f"the job uses {expect_tape} mm")
            if strict_tape:
                raise TapeMismatchError(message)
            logger.info("warning: " + message)
    return status


def wait_idle(transport, logger, timeout=IDLE_WAIT_TIMEOUT, step=0.2):
    """Wait in <=200 ms steps until ST in {00,05} and ER == 00 (PROTOCOL.md 3.13)."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        status = transport.read_status(timeout=min(step, remaining))
        if status is None:
            continue
        logger.info("status: " + status.summary())
        if status.er != 0x00:
            raise ProtocolError(f"printer error {status.er:02X}: {status.er_text}")
        if status.st == 0xFF:
            raise ProtocolError("printer reported unexpected error (ST=FF)")
        if status.st in IDLE_STATES:
            return True


def open_transport(mac=None, port=None, channel=None, logger=None,
                   connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT,
                   retry=True, link_settle_ms=LINK_SETTLE * 1000.0):
    """Pick the backend: `port` (serial) wins, else `mac` (Linux RFCOMM).

    Returns an unconnected transport; call .connect() (or use it as a context
    manager) and .close() when done.
    """
    logger = logger or Logger()
    if port:
        return SerialPortTransport(
            port,
            logger=logger,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            write_timeout=WRITE_TIMEOUT,
            band_write_timeout=BAND_WRITE_TIMEOUT,
            settle=max(0.0, link_settle_ms) / 1000.0,
        )
    if sys.platform == "darwin":
        raise ValueError(
            "Bluetooth RFCOMM sockets do not exist on macOS; pair the printer in "
            "System Settings -> Bluetooth, then pass --port /dev/cu.<printer> "
            "(`scan` lists the serial-port candidates)")
    if not mac:
        raise ValueError("no printer address; run `scan` and pass "
                         "--mac AA:BB:CC:DD:EE:FF (or --port /dev/cu.* to use a "
                         "serial port)")
    return RfcommTransport(
        normalize_mac(mac),
        channel=channel,
        logger=logger,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retry=retry,
    )


def make_transport(args, logger):
    """CLI wrapper around open_transport()."""
    return open_transport(
        mac=getattr(args, "mac", None),
        port=getattr(args, "port", None),
        channel=getattr(args, "channel", None),
        logger=logger,
        connect_timeout=args.connect_timeout,
        read_timeout=args.timeout,
        retry=getattr(args, "retry", True),
        link_settle_ms=getattr(args, "link_settle_ms", LINK_SETTLE * 1000.0),
    )


def send_job(transport, plan, logger):
    """Send a built JobPlan over a connected transport (session steps 6-9).

    Expects session_prepare() to have run. Waits for the printer to go idle
    after the job: closing the link early wedges the LW-C410 [HW-verified].
    Returns the last status frame seen (or None).
    """
    transport.write(plan.job_env, "job environment")
    transport.write(status_request_on(), "Q(05) status on (re-arm)")
    last_status = None

    for copy_index, page in enumerate(plan.pages, 1):
        logger.info(f"copy {copy_index}/{len(plan.pages)}")
        transport.write(page.page_env, f"page environment (copy {copy_index})")
        for band_index, (band, band_rows) in enumerate(
                zip(page.bands, page.band_rows), 1):
            transport.write(
                band,
                f"band {band_index}/{len(page.bands)} ({band_rows} rows)",
                timeout=BAND_WRITE_TIMEOUT)
            if plan.band_delay_ms > 0:
                time.sleep(plan.band_delay_ms / 1000.0)
            status = transport.read_status(timeout=BAND_STATUS_TIMEOUT)
            if status is None:
                logger.info("  warning: no status frame after band (continuing)")
                continue
            last_status = status
            logger.info("  status: " + status.summary())
            if status.er != 0x00:
                raise ProtocolError(
                    f"printer error during print {status.er:02X}: {status.er_text}")
            if status.st == 0xFF:
                raise ProtocolError("printer reported unexpected error (ST=FF)")
        transport.write(END_OF_PAGE, f"end of page (copy {copy_index})")

    transport.write(plan.job_end, "print end ('@')")
    time.sleep(0.5)     # after-print settle (PROTOCOL.md 3.13)
    if not wait_idle(transport, logger, timeout=IDLE_WAIT_TIMEOUT):
        logger.info(f"warning: printer did not report idle within "
                    f"{IDLE_WAIT_TIMEOUT:g} s")
    transport.write(status_request_off(), "Q(00) status off")
    return last_status


# ---------------------------------------------------------------------------
# Library API (render.py, the print service). Defaults are the settings
# verified on hardware in 2026-11: flip on, no mirror, 64-row bands, 500 ms
# band pacing, 30 s idle wait. One call = one Bluetooth session; the printer
# serves a single client, so callers must serialize calls.
# ---------------------------------------------------------------------------

LIB_MARGIN_MM = 2.0                 # lean but safe blank tape at each end


def bitmap_from_image(image, threshold=140, invert=False):
    """Pillow image -> Bitmap. Image width = along the tape, height = across."""
    gray = image.convert("L")
    width, height = gray.size
    if width <= 0 or height <= 0:
        raise ValueError("empty image")
    data = gray.tobytes()               # row-major, one byte per pixel
    px = [bytearray(height) for _ in range(width)]
    for y in range(height):
        base = y * width
        for x in range(width):
            luma = data[base + x]
            printed = luma >= threshold if invert else luma < threshold
            if printed:
                px[x][y] = 1
    return Bitmap(width, height, px)


def plan_bitmap(bitmap, tape_mm, margin_mm=LIB_MARGIN_MM, copies=1, cut="each",
                density=0, flip=True, mirror=False, band_rows=DEFAULT_BAND_ROWS,
                band_delay_ms=BAND_DELAY_MS):
    """Fit a Bitmap to the tape and build the JobPlan (no I/O)."""
    if margin_mm < 0:
        raise ValueError("margin_mm must be >= 0")
    if cut not in CUT_VECTORS:
        raise ValueError(f"cut must be one of {', '.join(sorted(CUT_VECTORS))}")
    if not 1 <= int(copies) <= 99:
        raise ValueError("copies must be between 1 and 99")
    if not -5 <= int(density) <= 5:
        raise ValueError("density must be between -5 and 5")
    dots = printable_dots(tape_mm)
    if bitmap.cols != dots:
        bitmap = resize_bitmap(bitmap, cols=dots)
    if flip:
        bitmap = Bitmap(bitmap.rows, bitmap.cols, px=bitmap.px[::-1])
    return build_job_plan(bitmap, tape_mm=tape_mm, margin_mm=margin_mm,
                          density=density, cut=cut, copies=copies,
                          band_rows=min(int(band_rows), MAX_BAND_ROWS),
                          mirror=mirror, band_delay_ms=band_delay_ms)


def open_session(transport, logger, **prepare_options):
    """connect() + session_prepare(). Any failure here happens before job data
    is sent and is re-raised as PrinterUnavailableError (retryable), except a
    TapeMismatchError."""
    try:
        transport.connect()
        return session_prepare(transport, logger, **prepare_options)
    except TapeMismatchError:
        raise
    except (TransportError, ProtocolError, OSError) as exc:
        raise PrinterUnavailableError(str(exc)) from exc


def print_bitmap(bitmap, tape_mm, mac=None, port=None, logger=None,
                 strict_tape=True, connect_timeout=CONNECT_TIMEOUT,
                 read_timeout=READ_TIMEOUT, **plan_options):
    """Print one Bitmap (natural orientation) in its own Bluetooth session.

    `plan_options` go to plan_bitmap() (margin_mm, copies, cut, density, flip,
    ...). With strict_tape the job is refused before any raster data is sent
    if the loaded cassette does not match `tape_mm` (TapeMismatchError).
    PrinterUnavailableError means nothing was printed and the call may be
    retried; any other error after that point may have printed partially.
    Returns (plan, status).
    """
    logger = logger or Logger()
    plan = plan_bitmap(bitmap, tape_mm, **plan_options)
    transport = open_transport(mac=mac, port=port, logger=logger,
                               connect_timeout=connect_timeout,
                               read_timeout=read_timeout)
    try:
        open_session(transport, logger, expect_tape=int(tape_mm),
                     strict_tape=strict_tape)
        status = send_job(transport, plan, logger)
        logger.info("Print job sent.")
        return plan, status
    finally:
        transport.close()


def query_status(mac=None, port=None, logger=None, reset=True,
                 connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT):
    """Open a session, return the printer's StatusFrame, close cleanly."""
    logger = logger or Logger()
    transport = open_transport(mac=mac, port=port, logger=logger,
                               connect_timeout=connect_timeout,
                               read_timeout=read_timeout)
    try:
        status = open_session(transport, logger, reset=reset,
                              status_timeout=max(10.0, read_timeout))
        transport.write(status_request_off(), "Q(00) status off")
        return status
    finally:
        transport.close()


def feed_tape(mac=None, port=None, cut=False, logger=None,
              connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT):
    """Feed a little tape (cut=True: feed + cut) in its own session."""
    logger = logger or Logger()
    transport = open_transport(mac=mac, port=port, logger=logger,
                               connect_timeout=connect_timeout,
                               read_timeout=read_timeout)
    try:
        open_session(transport, logger)
        if cut:
            transport.write(tape_feed_cut(), "feed + cut")
        else:
            transport.write(tape_feed(), "feed")
        if not wait_idle(transport, logger, timeout=IDLE_WAIT_TIMEOUT):
            logger.info(f"warning: printer did not report idle within "
                        f"{IDLE_WAIT_TIMEOUT:g} s")
        transport.write(status_request_off(), "Q(00) status off")
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def serial_port_candidates(match):
    """List serial ports; returns ([(path, name, cu_twin)], matching entries).

    /dev/cu.* comes first (the call-out device to use), /dev/tty.* second; a
    /dev/tty.* twin of an already listed /dev/cu.* device is reported once via
    the `cu_twin` path instead of as a second candidate.
    """
    needle = (match or "").lower()
    cu_paths = sorted(glob.glob("/dev/cu.*"))
    cu_names = {os.path.basename(path)[len("cu."):]: path for path in cu_paths}
    entries = [(path, os.path.basename(path)[len("cu."):], None) for path in cu_paths]
    for path in sorted(glob.glob("/dev/tty.*")):
        name = os.path.basename(path)[len("tty."):]
        entries.append((path, name, cu_names.get(name)))
    matches = [entry for entry in entries
               if needle and entry[2] is None and needle in entry[1].lower()]
    return entries, matches


def print_serial_ports(entries, matches, match):
    """Human-readable serial-port list; matching names are marked."""
    match_paths = {path for path, _name, _twin in matches}
    print(f"Serial port candidates; {len(matches)} matching '{match}':")
    for path, _name, twin in entries:
        if twin is not None:
            print(f"    {path}  (same device as {twin}; use the /dev/cu.* name)")
            continue
        hit = path in match_paths
        marker = "*" if hit else " "
        suffix = "   <-- match" if hit else ""
        print(f"  {marker} {path}{suffix}")
    if not entries:
        print("  (none found)")
    elif not match_paths:
        print(f"\nNo serial port matching '{match}'. Pair the printer first, then "
              "rerun scan; use --match to highlight another device name.")


def scan_macos(args, logger):
    """macOS discovery: paired devices via blueutil (optional) + /dev/cu.* ports."""
    if shutil.which("blueutil"):
        logger.info("Paired Bluetooth devices (blueutil --paired):")
        try:
            proc = subprocess.run(["blueutil", "--paired"], capture_output=True,
                                  text=True, timeout=30)
            output = (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"blueutil --paired failed: {exc}")
            output = ""
        for line in output.splitlines():
            if line.strip():
                print("  " + line.rstrip())
    else:
        logger.debug("blueutil not installed (optional, brew install blueutil)")

    entries, matches = serial_port_candidates(args.match)
    print_serial_ports(entries, matches, args.match)
    print("\nPair the printer in System Settings -> Bluetooth, then pass "
          "--port /dev/cu.<name>.")
    if not matches:
        return 1
    print(f"\nUse: --port {matches[0][0]}")
    return 0


def cmd_scan(args, logger):
    if sys.platform == "darwin":
        return scan_macos(args, logger)
    devices = scan_devices(args.scan_timeout, logger)
    match = args.match
    needle = match.lower()
    matches = [mac for mac in devices if needle in devices[mac].lower()]
    ordered = sorted(devices, key=lambda mac: (
        0 if mac in matches else 1, devices[mac].lower(), mac))

    print(f"Found {len(devices)} device(s); {len(matches)} matching '{match}':")
    for mac in ordered:
        name = devices[mac]
        hit = mac in matches
        if args.only and not hit:
            continue
        marker = "*" if hit else " "
        suffix = "   <-- match" if hit else ""
        print(f"  {marker} {mac}  {name or '(no name)'}{suffix}")
    if not matches:
        print(f"\nNo device matching '{match}' seen. The printer may be off, "
              "already connected to another host, or the scan was too short "
              "(try --scan-timeout 15).")
        return 1
    print(f"\nUse: --mac {matches[0]}")
    return 0


def cmd_status(args, logger):
    transport = make_transport(args, logger)
    try:
        with transport:
            status = session_prepare(transport, logger,
                                     reset=not args.no_reset,
                                     status_timeout=max(10.0, args.timeout))
            print_status_frame(status, title=f"{timestamp()}  printer status")
            transport.write(status_request_off(), "Q(00) status off")
            return 0
    finally:
        transport.close()


def cmd_monitor(args, logger):
    transport = make_transport(args, logger)
    try:
        transport.connect()
        if args.no_send:
            logger.info("Not sending anything; just listening for whatever the "
                        "printer pushes on link-up (Ctrl-C to stop) ...")
        else:
            transport.write(status_request_on(), "Q(05) status on")
            logger.info("Monitoring status frames (Ctrl-C to stop) ...")
        while True:
            frames, chunk = transport.poll(1.0)
            for status in frames:
                print_status_frame(status, title=f"{timestamp()}  status frame")
            if chunk is not None and not frames:
                print(f"{timestamp()}  RX raw (not a complete status frame): "
                      f"{hexs(chunk)}")
    except KeyboardInterrupt:
        logger.info("\nStopping monitor.")
        try:
            transport.write(status_request_off(), "Q(00) status off")
        except (TransportError, OSError):
            pass
    finally:
        transport.close()
    return 0


def cmd_feed(args, logger):
    transport = make_transport(args, logger)
    try:
        transport.connect()
        if not args.no_session:
            session_prepare(transport, logger)
        if args.cut:
            transport.write(tape_feed_cut(), "feed + cut")
        else:
            transport.write(tape_feed(), "feed")
        if not wait_idle(transport, logger, timeout=IDLE_WAIT_TIMEOUT):
            logger.info(f"warning: printer did not report idle within "
                        f"{IDLE_WAIT_TIMEOUT:g} s")
        if not args.no_session:
            transport.write(status_request_off(), "Q(00) status off")
    finally:
        transport.close()
    return 0


def parse_hex_args(parts):
    """Accept hex with or without spaces/commas/colons and 0x prefixes."""
    joined = "".join(parts) if not isinstance(parts, str) else parts
    cleaned = re.sub(r"\s+|0x|,|:|_", "", joined, flags=re.IGNORECASE)
    if not cleaned:
        raise ValueError("empty hex string")
    if len(cleaned) % 2:
        raise ValueError(f"hex string has an odd number of digits ({len(cleaned)})")
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise ValueError(f"invalid hex string: {joined!r}")


def cmd_raw(args, logger):
    data = parse_hex_args(args.hexbytes)
    repeats = max(1, int(args.repeat))
    logger.info(f"Sending {len(data)} byte(s) x{repeats}: {hexs(data)}")
    transport = make_transport(args, logger)
    try:
        transport.connect()
        received = 0
        for n in range(1, repeats + 1):
            transport.write(data, f"raw ({n}/{repeats})")
            if args.no_read:
                continue
            # listen between sends (and --wait s after the last one)
            listen_for = args.interval if n < repeats else args.wait
            end = time.monotonic() + max(0.0, float(listen_for))
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                frames, chunk = transport.poll(min(0.5, remaining))
                for status in frames:
                    print_status_frame(status,
                                       title=f"{timestamp()}  received status frame")
                    received += 1
                if chunk is not None and not frames:
                    print(f"{timestamp()}  RX raw (not a complete status "
                          f"frame): {hexs(chunk)}")
                    received += 1
        if received == 0 and not args.no_read:
            logger.info(f"No response (repeat={repeats}, interval={args.interval:g} s, "
                        f"wait={args.wait:g} s). The printer may not be hearing us "
                        "-- try `monitor --no-send`, or -v to hexdump all traffic.")
        return 0
    finally:
        transport.close()


def cmd_print(args, logger):
    if args.image and args.pattern:
        raise ValueError("use either -i/--image or -p/--pattern, not both")
    if not args.image and not args.pattern:
        raise ValueError("specify -i/--image FILE or -p/--pattern PATTERN")
    if args.margin_mm < 0:
        raise ValueError("--margin-mm must be >= 0")
    if args.length_mm is not None and args.length_mm <= 0:
        raise ValueError("--length-mm must be > 0")

    dots = printable_dots(args.tape)
    if args.image:
        bitmap = load_image(args.image, invert=args.invert, threshold=args.threshold)
        rows = mm_to_dots(args.length_mm) if args.length_mm is not None else None
        bitmap = resize_bitmap(bitmap, rows=rows, cols=dots)
    else:
        bitmap = make_pattern(args.pattern, args.tape, length_mm=args.length_mm)

    if args.flip:
        bitmap = Bitmap(bitmap.rows, bitmap.cols, px=bitmap.px[::-1])

    if args.band_rows > MAX_BAND_ROWS:
        logger.info(f"note: --band-rows clamped to {MAX_BAND_ROWS} "
                    "(protocol band limit)")
    plan = build_job_plan(
        bitmap,
        tape_mm=args.tape,
        margin_mm=args.margin_mm,
        density=args.density,
        cut=args.cut,
        copies=args.copies,
        band_rows=min(args.band_rows, MAX_BAND_ROWS),
        mirror=args.mirror,
        band_delay_ms=args.band_delay_ms,
    )
    report_plan(plan, logger)

    if args.dry_run:
        print("Dry run: not connecting.")
        return 0

    transport = make_transport(args, logger)
    try:
        transport.connect()
        session_prepare(transport, logger, expect_tape=args.tape)
        send_job(transport, plan, logger)
        logger.info("Print job sent.")
        return 0
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EXAMPLES = """\
examples:
  %(prog)s scan
  %(prog)s scan --scan-timeout 15 --only
  %(prog)s status  --mac AA:BB:CC:DD:EE:FF -v
  %(prog)s status  --port /dev/cu.LW-C410-SerialPort1 -v      (macOS, after pairing)
  %(prog)s monitor --port /dev/cu.LW-C410-SerialPort1 --no-send (macOS: just listen)
  %(prog)s raw     --port /dev/cu.LW-C410-SerialPort1 --repeat 3 1B 7B 05 51 05 00 56 7D
  %(prog)s monitor --mac AA:BB:CC:DD:EE:FF
  %(prog)s feed    --mac AA:BB:CC:DD:EE:FF
  %(prog)s cut     --mac AA:BB:CC:DD:EE:FF
  %(prog)s raw     --mac AA:BB:CC:DD:EE:FF --wait 3 1B7B0551050056 7D
  %(prog)s raw     --mac AA:BB:CC:DD:EE:FF 1B 7B 05 51 05 00 56 7D
  %(prog)s print   --dry-run -p bars --tape 12 --length-mm 25
  %(prog)s print   --dry-run -p checker --tape 18 --length-mm 40 -v
  %(prog)s print   --dry-run -i label.pbm --tape 12 --mirror
  %(prog)s print   --mac AA:BB:CC:DD:EE:FF -p "text HELLO" --tape 12

See ../docs/PROTOCOL.md for the byte-level protocol.
"""


def _bounded_int(name, low, high):
    def convert(text):
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be an integer")
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{name} must be between {low} and {high}")
        return value
    return convert


def _add_common_options(parser):
    """Options accepted both before and after the subcommand (SUPPRESS defaults)."""
    parser.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS,
                        help="hexdump every write/read with direction arrows")
    parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                        metavar="SEC",
                        help=f"read timeout in seconds (default {READ_TIMEOUT:g})")
    parser.add_argument("--connect-timeout", type=float, default=argparse.SUPPRESS,
                        metavar="SEC",
                        help=f"per-attempt connect watchdog (default {CONNECT_TIMEOUT:g})")
    parser.add_argument("--mac", metavar="AA:BB:CC:DD:EE:FF", default=argparse.SUPPRESS,
                        help="printer Bluetooth address (from `scan`)")
    parser.add_argument("--channel", type=int, default=argparse.SUPPRESS,
                        metavar="N", help="RFCOMM channel (default: SDP lookup, else 1)")
    parser.add_argument("--port", metavar="PATH", default=argparse.SUPPRESS,
                        help="serial port to use instead of Bluetooth RFCOMM "
                             "(macOS: /dev/cu.<printer> after pairing; also "
                             "USB/ESP32 serial bridges)")
    parser.add_argument("--link-settle-ms", type=float, default=argparse.SUPPRESS,
                        metavar="MS",
                        help=f"after opening a serial port, wait this long before "
                             f"the first write so the Bluetooth link can come up "
                             f"(default {LINK_SETTLE * 1000:g}; 0 disables)")
    parser.add_argument("--no-retry", action="store_true", default=argparse.SUPPRESS,
                        help="connect once instead of the app's 2x SDP + 2x channel-1 retry")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    _add_common_options(common)

    parser = argparse.ArgumentParser(
        prog="lwc410_bt.py",
        description="Epson LW-C410 label printer over Bluetooth Classic SPP (RFCOMM).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    _add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    scan = subparsers.add_parser(
        "scan", parents=[common], help="discover printers (bluetoothctl/hcitool)",
        description="Scan for nearby Bluetooth devices and highlight LW-C410 printers.")
    scan.add_argument("--scan-timeout", type=float, default=10.0, metavar="SEC",
                      help="scan duration in seconds (default 10)")
    scan.add_argument("--match", default="LW-C410", metavar="TEXT",
                      help="name substring to highlight (default LW-C410)")
    scan.add_argument("--only", action="store_true",
                      help="print only devices whose name matches")
    scan.set_defaults(func=cmd_scan)

    status = subparsers.add_parser(
        "status", parents=[common], help="reset, enable status and dump one frame",
        description="Session steps 2-5: Q(00), '!' reset, Q(05), decode status, Q(00).")
    status.add_argument("--no-reset", action="store_true",
                        help="skip the '!' printer reset frame (debug: does the "
                             "reset break the link?)")
    status.set_defaults(func=cmd_status)

    monitor = subparsers.add_parser(
        "monitor", parents=[common], help="print every status frame until Ctrl-C",
        description="Q(05), then decode every 64-byte status frame the printer pushes.")
    monitor.add_argument("--no-send", action="store_true",
                         help="send nothing; just hexdump whatever the printer "
                              "pushes on link-up")
    monitor.set_defaults(func=cmd_monitor)

    feed = subparsers.add_parser(
        "feed", parents=[common], help="send the feed-only frame",
        description="Small session, then 1B 7B 04 2B 00 2B 7D (feed).")
    feed.add_argument("--no-session", action="store_true",
                      help="just connect and send the frame (no Q(00)/! /Q(05))")
    feed.set_defaults(func=cmd_feed, cut=False)

    cut = subparsers.add_parser(
        "cut", parents=[common], help="send the feed+cut frame",
        description="Small session, then 1B 7B 04 2B 01 2C 7D (feed + cut).")
    cut.add_argument("--no-session", action="store_true",
                     help="just connect and send the frame (no Q(00)/! /Q(05))")
    cut.set_defaults(func=cmd_feed, cut=True)

    raw = subparsers.add_parser(
        "raw", parents=[common], help="send arbitrary hex bytes, optionally read",
        description="Send hex bytes (spaces optional), then print received status frames.")
    raw.add_argument("hexbytes", nargs="+", metavar="HEX",
                     help="hex bytes, e.g. 1B7B0551050056 7D or '1B 7B 05 51 05 00 56 7D'")
    raw.add_argument("--wait", type=float, default=2.0, metavar="SEC",
                     help="how long to listen after the LAST send (default 2)")
    raw.add_argument("--repeat", type=_bounded_int("--repeat", 1, 99), default=1,
                     metavar="N",
                     help="send the bytes N times (default 1); useful when the "
                          "first writes are lost while the link comes up")
    raw.add_argument("--interval", type=float, default=1.0, metavar="SEC",
                     help="seconds between repeats, spent listening (default 1)")
    raw.add_argument("--no-read", action="store_true",
                     help="send and close without reading anything")
    raw.set_defaults(func=cmd_raw)

    pr = subparsers.add_parser(
        "print", parents=[common], help="build and send a full print job",
        description="Full print stream: job env, page env, raster bands, 0C, '@'.")
    pr.add_argument("-i", "--image", metavar="FILE",
                    help="PBM (P1/P4) image; PNG only with Pillow installed. "
                         "Image width = along the tape, height = across the tape.")
    pr.add_argument("-p", "--pattern", metavar="NAME",
                    help="test pattern: lines | checker | bars | solid | 'text HELLO'")
    pr.add_argument("--tape", type=int, choices=TAPE_CHOICES, default=12, metavar="MM",
                    help="tape width in mm: 4/6/9/12/18 (default 12)")
    pr.add_argument("--length-mm", type=float, default=None, metavar="MM",
                    help="content length along the tape (patterns default 25; "
                         "images default to the image width)")
    pr.add_argument("--margin-mm", type=float, default=DEFAULT_MARGIN_MM, metavar="MM",
                    help=f"leading margin in mm (default {DEFAULT_MARGIN_MM:g} = 70 dots)")
    pr.add_argument("--density", type=_bounded_int("--density", -5, 5), default=0,
                    metavar="N", help="print density -5..5 (default 0)")
    pr.add_argument("--cut", choices=sorted(CUT_VECTORS), default="each",
                    help="cut mode: each (default), job, none")
    pr.add_argument("--copies", type=_bounded_int("--copies", 1, 99), default=1,
                    metavar="N", help="number of copies 1..99 (default 1)")
    pr.add_argument("--band-rows", type=_bounded_int("--band-rows", 1, 1024),
                    default=DEFAULT_BAND_ROWS, metavar="N",
                    help=f"raster rows per band, <=1024 (default {DEFAULT_BAND_ROWS}; "
                         "keep small - big single writes lose the label tail)")
    pr.add_argument("--band-delay-ms", type=_bounded_int("--band-delay-ms", 0, 60000),
                    default=BAND_DELAY_MS, metavar="MS",
                    help=f"pause after each band (default {BAND_DELAY_MS}, model 1300 pacing)")
    pr.add_argument("--band-status-timeout", type=float, default=BAND_STATUS_TIMEOUT,
                    metavar="SEC",
                    help=f"status wait after a band (default {BAND_STATUS_TIMEOUT:g})")
    pr.add_argument("--mirror", action="store_true",
                    help="reverse column order (PROTOCOL.md 6 orientation knob)")
    pr.add_argument("--flip", action="store_true",
                    help="reverse raster row order (along-tape axis); combine "
                         "with --mirror for a 180-degree rotation")
    pr.add_argument("--invert", action="store_true",
                    help="invert image threshold (dark background, light label)")
    pr.add_argument("--threshold", type=_bounded_int("--threshold", 0, 255), default=140,
                    metavar="N", help="PNG luma threshold, print if luma < N (default 140)")
    pr.add_argument("--dry-run", action="store_true",
                    help="build and report the byte stream without connecting")
    pr.set_defaults(func=cmd_print)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # Options are defined on both the main parser and each subparser with
    # SUPPRESS defaults, so a value given before the subcommand survives.
    args.verbose = bool(getattr(args, "verbose", False))
    args.timeout = float(getattr(args, "timeout", READ_TIMEOUT))
    args.connect_timeout = float(
        getattr(args, "connect_timeout", min(CONNECT_TIMEOUT, args.timeout)))
    args.mac = getattr(args, "mac", None)
    args.port = getattr(args, "port", None)
    args.channel = getattr(args, "channel", None)
    args.retry = not bool(getattr(args, "no_retry", False))
    args.link_settle_ms = float(getattr(args, "link_settle_ms",
                                        LINK_SETTLE * 1000.0))
    if args.link_settle_ms < 0:
        parser.error("--link-settle-ms must be >= 0")

    if args.mac and args.port:
        parser.error("use either --mac or --port")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    if args.connect_timeout <= 0:
        parser.error("--connect-timeout must be > 0")

    logger = Logger(args.verbose)
    try:
        return args.func(args, logger)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except ProtocolError as exc:
        print(f"protocol error: {exc}", file=sys.stderr)
        return 3
    except (TransportError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
