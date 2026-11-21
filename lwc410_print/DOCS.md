# LW-C410 Label Printer

Prints text, QR and Homebox labels on an **Epson LabelWorks LW-C410** over Bluetooth
Classic, using a Bluetooth adapter on the Home Assistant host (tested target: TP-Link
UB500). Home Assistant and Homebox talk to it over HTTP:

```
Homebox (NAS) ── label service + print button ─┐
Home Assistant ── rest_command ────────────────┤──► this add-on ──Bluetooth──► LW-C410
```

## Installation

Settings → Add-ons → Add-on store → ⋮ → **Repositories** → add
`https://github.com/timoles/Epson-LWC410-HA-Bluetooth-Integration`. Then install
**LW-C410 Label Printer** from the store. Home Assistant builds the image on the
host (~1–2 min). New versions appear as updates (⋮ → Check for updates forces a refresh).

If you installed it as a local add-on (copied into `/addons/lwc410_print`) before,
uninstall that copy: both would compete for the port and the printer. Note your
settings first, because they are not carried over.

## Configuration

| Option | Meaning |
|---|---|
| `printer_mac` | Bluetooth address of the printer. |
| `tape_mm` | Default cassette width. A job is refused (nothing printed) if the loaded cassette differs. |
| `margin_mm` | Blank tape before and after each label (default 2). |
| `api_token` | **Set this.** The service listens on your LAN; without a token anyone there can print. |
| `homebox_url` | Homebox address as your phone reaches it. It goes into the QR codes. |
| `homebox_api_url` | Optional address for API lookups (e.g. `http://192.168.1.10:3100` on the NAS). Defaults to `homebox_url`. |
| `homebox_api_key` | Optional Homebox API key (Homebox → Profile → API keys). Allows printing by asset id alone and shortens item/location QR codes. |
| `qr_uppercase` | Encode `/a/<asset id>` URLs in uppercase: same QR size, higher error correction. Homebox opens them (tested v0.27). |
| `retry_minutes` | If the printer is off or out of range, keep the job and retry for this long (default 120, 0 = fail immediately). |
| `retry_interval_s` | How often to try to reach the printer while a job waits (default 15). |
| `port` | HTTP port (default 7099), on the host network. |
| `log_level` | `debug` hexdumps all Bluetooth traffic. |

**Printer switched off?** Print anyway. The job waits (`state: waiting`) and the add-on
tries to reach the printer every `retry_interval_s`. Switch it on and the queued labels
print in order. Only failures *before* any label data was sent are retried: printer
off, out of range, not answering, cover open. A wrong cassette fails at once, and so
does a connection lost in the middle of a label, so nothing is printed twice.

First start: the log should say `RFCOMM sockets available`. Switch the printer on and
run a status query (see API). Only one Bluetooth client can talk to the printer at a
time: keep the Epson phone app disconnected.

## Homebox (Docker on the NAS)

Homebox ≥ 0.27 can use this add-on as its label renderer and printer. Its own
**Print label** button then prints on the LW-C410. Add to the Homebox container's
environment (replace the IP and token):

```yaml
HBOX_LABEL_MAKER_LABEL_SERVICE_URL: "http://192.168.1.50:7099/api/homebox/label/YOUR_TOKEN"
HBOX_LABEL_MAKER_PRINT_COMMAND: "wget -q -O /dev/null --header=X-Api-Key:YOUR_TOKEN --post-file={{.FileName}} http://192.168.1.50:7099/api/print/image"
```

- In docker-compose's **list** syntax (`- KEY=value`) do not quote the value: the quotes
  become part of it (`failed to parse service URL ''http://…''`). Either write
  `- HBOX_LABEL_MAKER_LABEL_SERVICE_URL=http://…` without quotes, or use the map
  syntax shown above.
- The token goes **in the path** of the label-service URL because Homebox replaces
  that URL's query string with its own parameters.
- The print command is split on spaces, not run in a shell. So: no quotes, and the
  header has no space after the colon. `wget` is part of the Homebox image.
- Homebox sends relative QR URLs (`/a/000-042`); the add-on prefixes `homebox_url`.
- The Homebox preview shows the real tape label. Asset labels encode
  `/a/<asset id>`. Item and location labels encode `/item/<uuid>` or `/location/<uuid>`,
  which only fit as a dense QR code. With `homebox_api_key` set, the add-on replaces
  them with the asset URL.

## Home Assistant

`secrets.yaml`:

```yaml
label_printer_auth: "Bearer YOUR_TOKEN"
```

`configuration.yaml` (or a package). HA and the add-on both use the host network, so
`127.0.0.1` works:

```yaml
rest_command:
  label_print_text:
    url: http://127.0.0.1:7099/api/print/text
    method: POST
    headers:
      Authorization: !secret label_printer_auth
    content_type: application/json
    payload: >-
      {{ {"text": text, "copies": copies | default(1) | int} | tojson }}
  label_print_homebox:
    url: http://127.0.0.1:7099/api/print/homebox
    method: POST
    headers:
      Authorization: !secret label_printer_auth
    content_type: application/json
    payload: >-
      {{ {"asset_id": asset_id | string, "copies": copies | default(1) | int} | tojson }}
  label_feed_cut:
    url: http://127.0.0.1:7099/api/cut
    method: POST
    headers:
      Authorization: !secret label_printer_auth
  label_queue_clear:
    url: http://127.0.0.1:7099/api/queue/clear
    method: POST
    headers:
      Authorization: !secret label_printer_auth
  label_status_refresh:
    url: http://127.0.0.1:7099/api/status/refresh
    method: POST
    headers:
      Authorization: !secret label_printer_auth

rest:
  - resource: http://127.0.0.1:7099/api/status
    headers:
      Authorization: !secret label_printer_auth
    scan_interval: 30          # cached state only, does not wake the printer
    sensor:
      - name: Label printer
        unique_id: lwc410_label_printer
        icon: mdi:label-outline
        value_template: >-
          {%- if value_json.busy -%}printing
          {%- elif value_json.waiting_for_printer -%}waiting for printer
          {%- elif value_json.last_error -%}error
          {%- elif value_json.printer -%}{{ value_json.printer.state }}
          {%- else -%}unknown{%- endif -%}
        json_attributes:
          - pending
          - queued
          - last_error
          - printer
          - printed_since_start

script:
  print_label:
    alias: Print label
    icon: mdi:printer
    fields:
      text:
        name: Text
        description: One line per row, at most 4
        required: true
        selector:
          text:
            multiline: true
      copies:
        name: Copies
        default: 1
        selector:
          number: {min: 1, max: 10}
    sequence:
      - action: rest_command.label_print_text
        data:
          text: "{{ text }}"
          copies: "{{ copies | default(1) }}"
  print_homebox_label:
    alias: Print Homebox label
    icon: mdi:qrcode
    fields:
      asset_id:
        name: Asset ID
        description: e.g. 42 or 000-042
        required: true
        selector:
          text:
    sequence:
      - action: rest_command.label_print_homebox
        data:
          asset_id: "{{ asset_id }}"
```

Put the scripts on a dashboard. Tapping one opens a dialog with its fields.
`print_homebox_label` needs `homebox_api_key`, or pass `name`/`location` in the payload.

## HTTP API

Authentication (if `api_token` is set): `Authorization: Bearer <token>`,
`X-Api-Key: <token>` or `?key=<token>`.

| Request | Body (JSON) / notes |
|---|---|
| `POST /api/print/text` | `{"text": "line 1\nline 2"}` or `{"lines": [...]}`, optional `length_mm` |
| `POST /api/print/qr` | `{"data": "...", "lines": [...], "uppercase": false}` |
| `POST /api/print/homebox` | `{"asset_id": "42"}` or `{"item_id": "<uuid>"}`, optional `name`, `location` |
| `POST /api/print/image` | Raw PNG/JPEG body, scaled to the tape height; `?rotate=1` for portrait |
| `POST /api/feed`, `POST /api/cut` | Feed a little tape / feed and cut |
| `POST /api/status/refresh` | Queue a status query of the printer |
| `GET /api/status` | Cached state: busy, `waiting_for_printer`, `pending` jobs, last error, last printer status |
| `GET /api/jobs/<id>` | Job state: `queued`, `running`, `waiting` (printer unreachable, retrying), `done`, `failed`, `cancelled` |
| `POST /api/jobs/<id>/cancel` | Cancel a queued or waiting job (409 while it is printing) |
| `POST /api/queue/clear` | Cancel all queued and waiting jobs |
| `GET /api/preview/<text\|qr\|homebox>?...` | Label PNG for query parameters, no printing |
| `GET /api/homebox/label[/<token>]` | Homebox label service (see above) |
| `GET /api/health` | Liveness, no auth |

Common print fields: `tape_mm`, `copies` (1–20), `margin_mm`, `cut` (`each`/`job`/`none`),
`density` (−5…5), `max_length_mm`. `"preview": true` returns the PNG instead of
printing. `"wait": true` blocks until the job finishes (10–40 s, at most 180 s; still
waiting for the printer → 202), otherwise you get `202 {"id": ...}` immediately.

Example:

```sh
curl -X POST http://HA-IP:7099/api/print/text -H "Authorization: Bearer TOKEN" \
     -d '{"text": "Kabel\nKiste 7", "wait": true}'
```

## Troubleshooting

- **`cannot create an RFCOMM socket`** in the log: the host kernel lacks Bluetooth
  RFCOMM support, or the add-on runs without `host_network`.
- **Jobs stay `waiting`:** the printer is off (it auto-powers off after ~20 min idle),
  out of range (~10 m from the adapter), or wedged. Switch it on or power-cycle it.
  The log shows the reason once per job (`log_level: debug` shows every attempt).
- **Printer silent until power-cycled:** it got wedged by a broken session (two
  clients, phone app connected). Power-cycle it.
- **`printer reports 18 mm tape, the job uses 12 mm`:** change `tape_mm` or pass it per request.
- **QR does not scan:** labels below 3 dots/module (shown in `X-Label-Info` of a
  preview) are denser than what was tested on hardware. Use asset ids, `qr_uppercase`,
  or 18 mm tape.
