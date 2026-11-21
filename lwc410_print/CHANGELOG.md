# Changelog

## 0.3.0

- Jobs wait for the printer: if it is off or out of range, the job is kept and
  retried every `retry_interval_s` for up to `retry_minutes`; queued labels print
  as soon as it is switched on. Never retried once data was sent (no double prints).
- New: `POST /api/jobs/<id>/cancel`, `POST /api/queue/clear`; status shows
  `waiting_for_printer` and `pending`.

## 0.2.0

- Homebox labels: item name on two rows, asset id + location in a row at half
  the previous font size. Labels get considerably shorter.

## 0.1.0

- First version: HTTP print service with job queue, text/QR/Homebox/image labels,
  previews, feed/cut, cached status, optional Homebox lookup and API token.
