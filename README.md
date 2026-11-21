# Epson LW-C410 Bluetooth label printer for Home Assistant

A Home Assistant add-on that prints labels on an **Epson LabelWorks LW-C410** over
Bluetooth Classic, directly from the Home Assistant host. No phone app, no cloud.

- Text labels, QR labels and **Homebox** inventory labels (QR to the item + name +
  asset id + location), rendered for 4–18 mm tape.
- HTTP API for Home Assistant (`rest_command`), with a status sensor.
- **Homebox ≥ 0.27 integration**: the add-on acts as Homebox's label renderer, so
  Homebox's own *Print label* button prints on the LW-C410.
- Jobs queue while the battery-powered printer is switched off and print as soon as it
  is switched on.

```
Homebox ── label service + print command ─┐
Home Assistant ── rest_command ───────────┤──► add-on ──Bluetooth──► LW-C410
```

## Installation

Requirements: Home Assistant OS (amd64 or aarch64) with a Bluetooth adapter that
supports Bluetooth Classic (e.g. TP-Link UB500), within range of the printer.

1. Settings → Add-ons → Add-on store → ⋮ → **Repositories** → add
   `https://github.com/timoles/Epson-LWC410-HA-Bluetooth-Integration`.
2. Install **LW-C410 Label Printer**, set the printer's Bluetooth address, an API token
   and your Homebox URL, then start it.

Configuration, the Home Assistant YAML, the Homebox settings and the HTTP API are
documented in [`lwc410_print/DOCS.md`](lwc410_print/DOCS.md). Home Assistant shows
the same text on the add-on's Documentation tab.

## Repository layout

| Path | What it is |
|---|---|
| `repository.yaml` | Home Assistant add-on repository metadata |
| `lwc410_print/` | The add-on: `config.yaml`, `Dockerfile`, `DOCS.md`, `app/` |
| `lwc410_print/app/server.py` | HTTP service, job queue, retries, Homebox label service |
| `lwc410_print/app/homebox.py` | Read-only Homebox API client (API key) |
| `python/lwc410_bt.py` | Printer protocol + Bluetooth transport, CLI (`scan`, `status`, `print`, …) |
| `python/render.py` | Label templates (Pillow + segno), CLI with previews |
| `python/btsnoop_parse.py` | Decoder for Android HCI snoop logs (protocol debugging) |
| `sync.sh` | Copies `python/lwc410_bt.py` and `render.py` into `lwc410_print/app/` |

The add-on can be used without Home Assistant: `python/` runs standalone on any
Linux machine with BlueZ. See [`python/README.md`](python/README.md).

## Development

- `python/` is the canonical source of `lwc410_bt.py` and `render.py`. After changing
  them run `./sync.sh`. Then bump `version` in `lwc410_print/config.yaml` and push.
  Home Assistant offers the new version as an update.
- Test the add-on without a printer on any Docker host:

  ```sh
  cd lwc410_print && docker build -t lwc410-print-test .
  mkdir -p /tmp/lwc410-data
  echo '{"api_token":"t","homebox_url":"https://homebox.example.com"}' > /tmp/lwc410-data/options.json
  docker run --rm -p 7099:7099 -e LWC410_SIMULATE=1 -v /tmp/lwc410-data:/data:ro lwc410-print-test
  curl -H 'X-Api-Key: t' 'http://127.0.0.1:7099/api/preview/text?text=Hello' -o label.png
  ```

  `LWC410_SIMULATE_OFFLINE=/data/offline` makes the simulated printer count as switched
  off while that file exists, to test the waiting queue.
- The base image is pinned by digest. Python dependencies are pinned with hashes
  (`lwc410_print/requirements.txt`, taken from `python/uv.lock`).

## Status

Verified on hardware (2026-11): Bluetooth printing from Linux with the Python tool
(text, QR, image labels). Verified in Docker: the add-on on amd64 and aarch64, the API,
the queue and retries (simulated printer), and the full round trip with Homebox v0.27.1.
Not yet verified on Home Assistant OS itself: RFCOMM support of the HA OS kernel and
adapter power-on via `bluetoothctl`.

## License

[MIT](LICENSE).

Not affiliated with or endorsed by Seiko Epson. "Epson" and "LabelWorks" are
trademarks of Seiko Epson Corporation. The protocol was reverse-engineered for
interoperability.
