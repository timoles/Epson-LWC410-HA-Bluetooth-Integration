# lwc410_bt.py — standalone Bluetooth tool for the Epson LW-C410

Python 3 (stdlib only) client for the LW-C410 label printer over Bluetooth
Classic SPP. On Linux it uses RFCOMM sockets (BlueZ); on macOS it drives the
serial port that macOS creates for the paired printer (`--port /dev/cu.*`).
Built from a reverse-engineered description of the printer's protocol; section
references such as "PROTOCOL.md 5.4" in the code point to those notes, which are
not part of this repository. It debugs the connection/protocol and sends complete
standalone print jobs — **verified on real hardware (2026-11)**.

## Running with uv (virtual env)

This folder is a [uv](https://docs.astral.sh/uv/) project (`pyproject.toml`,
`uv.lock`). The script itself needs **no third-party packages** — `uv sync`
just creates the `.venv`:

```sh
cd lwc410-esphome/python
uv sync                        # create/update .venv (no deps, instant)
uv run lwc410_bt.py scan       # … every command prefixed with: uv run
```

PNG image input (optional extra, installs Pillow into the venv):

```sh
uv sync --extra png            # or one-off: uv run --extra png lwc410_bt.py …
uv run lwc410_bt.py print --dry-run -i label.png --tape 12
```

Plain `python3` from any venv/interpreter ≥ 3.9 still works unchanged:
`./lwc410_bt.py …`.

Requirements, Linux: BlueZ (`bluetoothctl`/`hcitool` for `scan`, `sdptool`
optional for the SPP channel lookup), a Bluetooth adapter with Bluetooth Classic
support, and an RFCOMM-capable kernel socket module (`AF_BLUETOOTH` — standard on
Linux). No pairing needed (the printer accepts insecure RFCOMM, like the Android
app). The printer's SPP is on server channel 6 (verified via HCI snoop); connect
order is: SDP result → channel 6 → channel 1, overridable with `--channel`.
On Linux there is no macOS-style serial-port indirection — the socket API gives
real connect errors, and `btmon` shows live HCI traffic if you need to debug.
A Raspberry Pi (3/4/5, onboard BT) makes a fine dedicated print server.

```sh
uv run lwc410_bt.py scan                                  # find the printer MAC
uv run lwc410_bt.py status  --mac AA:BB:CC:DD:EE:FF -v    # session steps 2-5 + decoded status
uv run lwc410_bt.py monitor --mac AA:BB:CC:DD:EE:FF       # live status frames (debug workhorse)
uv run lwc410_bt.py feed    --mac AA:BB:CC:DD:EE:FF       # feed a little tape
uv run lwc410_bt.py cut     --mac AA:BB:CC:DD:EE:FF       # feed + cut
uv run lwc410_bt.py raw     --mac AA:BB:CC:DD:EE:FF 1B 7B 05 51 05 00 56 7D   # arbitrary bytes
uv run lwc410_bt.py status  --port /dev/cu.LW-C410-SerialPort1 -v            # macOS / USB serial

uv run lwc410_bt.py print --dry-run -p bars --tape 12 --length-mm 25          # byte-exact preview
uv run lwc410_bt.py print --mac AA:BB:CC:DD:EE:FF -p "text HELLO" --tape 12 --flip   # real print (--flip!)
uv run --extra png lwc410_bt.py print --mac AA:BB:CC:DD:EE:FF -i label.png --tape 18 --flip
uv run --extra render render.py qr "https://example.com" "Hello" --tape 12 --print --mac AA:BB:CC:DD:EE:FF
```

## Label templates: `render.py` (text, QR, Homebox)

`render.py` renders labels with TrueType text (Pillow, no anti-aliasing) and QR
codes (segno), then prints them through the library API of `lwc410_bt.py`
(`print_bitmap`, `query_status`, `feed_tape`). The hardware-verified settings are
built in (flip on, no mirror, 64-row bands, 500 ms pacing, 30 s idle wait), and a
job is refused before any raster data is sent if the loaded cassette does not match
`--tape`.

```sh
uv sync --extra render
uv run render.py text "Kabel & Adapter" "Kiste 7" --tape 12 -o label.png --preview
uv run render.py homebox --base-url https://hb.lan --asset-id 42 \
    --name "Akkuschrauber" --location "Garage" --tape 12 --upper -o label.png
uv run render.py homebox ... --print --dry-run                  # job bytes, no connect
uv run render.py homebox ... --print --mac AA:BB:CC:DD:EE:FF    # real print (Linux)
```

The QR uses whole dots per module when 3 or more fit (3 dots = 0.42 mm, the size
verified on hardware). Otherwise it is scaled fractionally to the tape width: module
edges are rounded to the dot grid, so modules are 2 or 3 dots wide. Below 2 dots per
module the payload is rejected. A `https://<host>/a/000-042` URL (30-36 characters)
needs QR version 2-3, which gives ~2.6 dots/module on 12 mm tape and 3 on 18 mm.
`--upper` encodes the URL in uppercase (alphanumeric QR mode): the same size, but with
more error correction. Homebox v0.27 opens uppercase `/A/…` URLs (tested). Item UUID URLs
(about 60 characters) are too long for small labels, so use asset ids. Font:
`--font` or `$LWC410_FONT`, otherwise DejaVu Sans Bold / Arial Bold, otherwise
Pillow's built-in font. The Home Assistant add-on in
[`../lwc410_print/`](../lwc410_print/DOCS.md) wraps both modules in an HTTP print
service (copied there by `../sync.sh`).

## macOS

macOS has no Bluetooth Classic socket API at all (no `AF_BLUETOOTH`/RFCOMM in the
kernel interfaces Python can reach), and [pybluez](https://github.com/pybluez/pybluez)
is unmaintained (last release 2020, pins pyobjc 5.3, breaks on current Python and
setuptools) — so neither the Linux socket path nor pybluez is an option. Instead:

1. Pair the printer once in **System Settings → Bluetooth** (click *Connect* on
   the LW-C410). What macOS asks for during pairing is **[unverified on
   hardware]** — the Android app never pairs (insecure RFCOMM, no PIN); expect
   either "just works" pairing or a code (try `0000` if prompted).
2. macOS then exposes the SPP link as a serial device. Find its name:
   `uv run lwc410_bt.py scan` lists every `/dev/cu.*` + `/dev/tty.*` candidate
   and highlights names containing `LW-C410` (and shows `blueutil --paired`
   output if `brew install blueutil` is present).
3. Use the `/dev/cu.*` name (call-out device) with `--port`:

```sh
uv run lwc410_bt.py status --port /dev/cu.LW-C410-SerialPort1 -v
uv run lwc410_bt.py print  --port /dev/cu.LW-C410-SerialPort1 -p "text HELLO" --tape 12
```

The exact port name depends on what macOS names the device
(`/dev/cu.<something>`, often with a `SerialPort` suffix) and is
**[unverified on hardware]** — run `scan` to see the real name. The port only
exists while the printer is paired and connected; re-pair after moving the
printer to another host.

The same `--port` backend works on Linux, which is handy for USB-serial
adapters and ESP32 serial bridges (any `/dev/ttyUSB*`, `/dev/ttyACM*`,
`/dev/pts/*`); it is plain `os.open`/`termios`/`select`, no `pyserial` needed.

Images: PBM (P1/P4) always; PNG with the `png` extra (Pillow; threshold: print if
luma < 140). Width = along the tape, height = across the tape. Test patterns: `lines`, `checker`, `bars`, `solid`, `text <TEXT>` (built-in
5×7 font, auto-scaled to the tape width). All patterns carry an asymmetric
corner marker to verify the (hardware-unconfirmed) mirroring question — use
`--mirror` if the first test label comes out flipped.

Notes:
- **[HW-verified 2026-11]** `--flip` (reverse raster row order) is required for correct
  reading orientation; do **not** use `--mirror` on the LW-C410 (letters come out mirrored).
- **[HW-verified 2026-11]** `--band-rows` defaults to **64**: single large band writes
  (> ~5.6 KB) make the printer drop the tail of the label. Keep bands small.
- **[HW-verified 2026-11]** after a job the tool waits up to 30 s for the printer to go idle
  before closing the link (like the app). Do not close the connection early — the printer
  wedges (silent until power-cycled).
- The printer serves **one RFCOMM client**: never run two commands concurrently and keep
  the Epson phone app away. It also auto-powers-off after ~20 min idle.
- `--band-delay-ms` defaults to 500 ms (model-1300 band pacing, PROTOCOL.md §5.5).
  Try lowering it once things work and report what the printer tolerates.
- Full `--help` on each subcommand; `-v` hexdumps every write/read.

## Troubleshooting: Linux / real hardware [HW-verified 2026-11]

- **Connects but no status frame / printer stops advertising after a print**: the printer
  is wedged. Cause is an unclean session close or two clients racing. Power-cycle the
  printer; a `!` reset does not revive it. The tool now waits 30 s after jobs, so this
  should not happen with it.
- **Nothing connects at all**: the printer auto-powered-off (idle ~20 min) — press power.
  If a scan still does not list it while other devices appear, it is wedged → power cycle.
- **After a VM suspend/resume** the USB dongle can vanish (`lsusb`) — re-attach it in the
  hypervisor, then `bluetoothctl power on`.

## Troubleshooting: "no status frame received after Q(05)"

The serial link opens (macOS `--port`), but the printer never answers. Ordered
things to try — each one distinguishes a different failure:

1. **Just re-run it.** The session now settles 500 ms after opening the port
   and re-sends `Q(05)` up to 3×, because macOS Bluetooth serial ports can
   silently drop the first writes while the RFCOMM channel is still coming up
   (`--link-settle-ms` tunes the settle time, `0` disables).
2. **Does the printer talk at all?** `uv run lwc410_bt.py monitor --port … --no-send`
   — sends nothing, prints every byte the printer pushes on link-up (raw hex
   if it is not a 64-byte status frame).
3. **Is it hearing us?** `uv run lwc410_bt.py raw --port … --repeat 3 1B 7B 05 51 05 00 56 7D`
   — sends `Q(05)` three times, shows any reply (valid frame or raw hex).
4. **Does the `!` reset break the link?** `status --no-reset` skips it.
5. Run everything with **`-v`** to hexdump both directions, and make sure the
   Epson app on a nearby phone is not connected to the printer at the same
   time (only one RFCOMM client is served).

If `raw --repeat` shows the printer replying with something that is *not* a
64-byte `@`…`0xFF` frame, capture that hex — the status format on this model
would then differ from the decompiled app path and is worth a look.

## Analyzing an Android HCI capture

`btsnoop_parse.py` (same folder, stdlib only) decodes an Android
`btsnoop_hci.log` — the SDP lookup, the RFCOMM frames, credit flow, and the
full ESC/P session with timestamps and rendered status frames. Extract the log
from a bugreport (`FS/data/misc/bluetooth/logs/btsnoop_hci.log`):

```sh
uv run btsnoop_parse.py path/to/btsnoop_hci.log            # full overview
uv run btsnoop_parse.py path/to/btsnoop_hci.log --data-only # just the ESC/P stream
```
