#!/usr/bin/env python3
"""HTTP print service for the Epson LW-C410 (Home Assistant add-on).

Contract:
- One worker thread owns the printer: jobs are queued and run strictly one
  after another (the LW-C410 serves a single RFCOMM client and wedges on
  overlapping or prematurely closed sessions).
- Print/feed/cut requests return 202 + a job id immediately (a print takes
  10-40 s incl. the 30 s idle wait); add "wait": true to block until done.
- Printer off or out of reach (anything that fails before job data is sent):
  the job state becomes "waiting" and it is retried every retry_interval_s
  for up to retry_minutes; later jobs stay queued behind it. Errors after
  data was sent are never retried (no double prints). POST
  /api/jobs/<id>/cancel and /api/queue/clear cancel queued/waiting jobs.
- GET /api/status answers from the cached result of the last job and never
  touches the printer; POST /api/status/refresh queues a real status query.
- "preview": true on a print request returns the rendered PNG, no printing.
- GET /api/homebox/label implements Homebox's external label service
  (HBOX_LABEL_MAKER_LABEL_SERVICE_URL): Homebox passes TitleText,
  DescriptionText and URL, we return the label PNG at tape height. Homebox's
  print button then runs HBOX_LABEL_MAKER_PRINT_COMMAND, which POSTs that PNG
  to /api/print/image (wget --post-file).
- If api_token is set, every /api/ request except /api/health needs
  "Authorization: Bearer <token>", "X-Api-Key: <token>" or ?key=<token>
  (Homebox's print command cannot send a header with spaces) or, for the
  label service only, /api/homebox/label/<token> (Homebox replaces the query
  string of that URL).

Config: /data/options.json (written by the Supervisor), path overridable via
LWC410_OPTIONS for local testing. LWC410_SIMULATE=1 builds jobs without
connecting (for tests without a printer); with LWC410_SIMULATE_OFFLINE=<file>
the simulated printer is "off" while that file exists.
"""

import hmac
import io
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import homebox  # noqa: E402
import lwc410_bt as lw  # noqa: E402
import render  # noqa: E402
from PIL import Image  # noqa: E402

log = logging.getLogger("lwc410")

MAX_BODY = 8 * 1024 * 1024
MAX_LINES = 4
MAX_LINE_CHARS = 200
MAX_QR_CHARS = 500
KEEP_JOBS = 50
SIMULATE = os.environ.get("LWC410_SIMULATE") == "1"
# Test aid: in simulate mode the printer counts as "off" while this file exists.
SIMULATE_OFFLINE = os.environ.get("LWC410_SIMULATE_OFFLINE", "")


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

DEFAULTS = {
    "printer_mac": "",
    "tape_mm": 12,
    "margin_mm": 2.0,
    "api_token": "",
    "homebox_url": "",
    "homebox_api_url": "",
    "homebox_api_key": "",
    "qr_uppercase": False,
    "retry_minutes": 120,
    "retry_interval_s": 15,
    "port": 7099,
    "log_level": "info",
}


def load_options():
    path = os.environ.get("LWC410_OPTIONS", "/data/options.json")
    options = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as handle:
            options.update({k: v for k, v in json.load(handle).items() if v is not None})
    except FileNotFoundError:
        log.warning("no options file at %s, using defaults", path)
    options["tape_mm"] = int(options["tape_mm"])
    options["margin_mm"] = float(options["margin_mm"])
    options["port"] = int(options["port"])
    options["retry_minutes"] = int(options["retry_minutes"])
    options["retry_interval_s"] = int(options["retry_interval_s"])
    return options


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

class BadRequest(ValueError):
    pass


def _str(body, key, required=False, limit=MAX_LINE_CHARS):
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BadRequest(f"'{key}' is required")
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise BadRequest(f"'{key}' must be a string")
    if len(value) > limit:
        raise BadRequest(f"'{key}' is longer than {limit} characters")
    return value


def _lines(body, required=True):
    """'lines': [..] or 'text': 'a\\nb' -> list of strings."""
    lines = body.get("lines")
    if lines is None:
        text = _str(body, "text", limit=MAX_LINES * MAX_LINE_CHARS)
        lines = text.splitlines() if text else []
    if not isinstance(lines, list) or not all(isinstance(x, (str, int, float)) for x in lines):
        raise BadRequest("'lines' must be a list of strings")
    lines = [str(x) for x in lines if str(x).strip()]
    if len(lines) > MAX_LINES:
        raise BadRequest(f"at most {MAX_LINES} lines")
    if any(len(x) > MAX_LINE_CHARS for x in lines):
        raise BadRequest(f"lines are limited to {MAX_LINE_CHARS} characters")
    if required and not lines:
        raise BadRequest("'text' or 'lines' is required")
    return lines


def _int(body, key, default, low, high):
    value = body.get(key, default)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise BadRequest(f"'{key}' must be an integer between {low} and {high}")
    return value


def _float(body, key, default, low, high):
    value = body.get(key, default)
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise BadRequest(f"'{key}' must be a number")
    if not low <= value <= high:
        raise BadRequest(f"'{key}' must be between {low} and {high}")
    return value


def _bool(body, key, default=False):
    value = body.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _tape(body, options):
    tape = _int(body, "tape_mm", options["tape_mm"], 0, 99)
    if tape not in lw.TAPE_DOTS:
        raise BadRequest(f"'tape_mm' must be one of {lw.TAPE_CHOICES}")
    return tape


def print_options(body, options):
    cut = body.get("cut", "each")
    if cut not in lw.CUT_VECTORS:
        raise BadRequest("'cut' must be each, job or none")
    return {
        "tape_mm": _tape(body, options),
        "copies": _int(body, "copies", 1, 1, 20),
        "margin_mm": _float(body, "margin_mm", options["margin_mm"], 0.0, 20.0),
        "cut": cut,
        "density": _int(body, "density", 0, -5, 5),
    }


# ---------------------------------------------------------------------------
# Rendering per job kind (no printer I/O)
# ---------------------------------------------------------------------------

class Renderer:
    def __init__(self, options):
        self.options = options
        self.homebox = None
        if options["homebox_api_key"]:
            self.homebox = homebox.HomeboxClient(
                options["homebox_api_url"] or options["homebox_url"],
                options["homebox_api_key"])

    def render(self, kind, body, tape):
        """Return (PIL image, info dict)."""
        max_len = _float(body, "max_length_mm", render.DEFAULT_MAX_LENGTH_MM, 10, 300)
        if kind == "text":
            image = render.render_text(
                _lines(body), tape, length_mm=_float(body, "length_mm", None, 5, 300),
                max_length_mm=max_len)
            return image, {}
        if kind == "qr":
            image, info = render.render_qr(
                _str(body, "data", required=True, limit=MAX_QR_CHARS), tape,
                _lines(body, required=False), max_length_mm=max_len,
                uppercase=_bool(body, "uppercase", False))
            return image, info
        if kind == "homebox":
            return self.render_homebox(body, tape, max_len)
        if kind == "homebox-service":
            return self.render_label_service(body, tape, max_len)
        if kind == "image":
            image = render.fit_image(body["_image"], tape, rotate=_bool(body, "rotate"))
            return image, {}
        raise BadRequest(f"unknown label kind {kind!r}")

    def render_label_service(self, body, tape, max_len):
        """Homebox label-service request -> our layout.

        What Homebox v0.27 sends (verified):
          asset label:    TitleText=<asset id>, DescriptionText="<name>\nLocation: <loc>",
                          URL=/a/<asset id>
          item label:     TitleText=<name>, DescriptionText="\nLocation: <loc>",
                          URL=/item/<uuid>
          location label: TitleText=<name>, DescriptionText="Homebox Location",
                          URL=/location/<uuid>
        URLs are relative unless Homebox knows its hostname. With an API key,
        /item/<uuid> and /location/<uuid> are replaced by the much shorter
        /a/<asset id> URL when the entity has an asset id.
        """
        url = _str(body, "URL", required=True, limit=MAX_QR_CHARS)
        base = self.options["homebox_url"]
        if url.startswith("/"):
            if not base:
                raise BadRequest("homebox_url is not configured (Homebox sent a "
                                 "relative URL)")
            url = base.rstrip("/") + url
        title = (_str(body, "TitleText", limit=MAX_LINE_CHARS) or "").strip()
        desc = [x.strip() for x in (_str(body, "DescriptionText",
                                         limit=4 * MAX_LINE_CHARS) or "").splitlines()
                if x.strip()]
        asset_id = title if len(title) == 7 and render.ASSET_ID_RE.match(title) else None

        item = render.ITEM_URL_RE.search(url)
        if item and not asset_id and self.homebox is not None and base:
            try:
                found = self.homebox.lookup(item_id=item.group(1)).get("asset_id")
            except homebox.HomeboxError as exc:
                log.warning("asset id lookup for %s failed: %s", item.group(1), exc)
                found = None
            if found:
                asset_id = found
                url = render.homebox_url(base, asset_id=found)

        if title and title != asset_id:
            name, details = title, desc
        else:
            name, details = (desc[0] if desc else title), desc[1:]
        details = [render.strip_field_label(x) for x in details]
        second = [x for x in [asset_id] + details if x]
        lines, heights = render.name_detail_layout(
            name, "  ".join(second), lw.printable_dots(tape), render.default_font_path())
        uppercase = _bool(body, "uppercase", self.options["qr_uppercase"]) and \
            render.is_asset_url(url)
        image, info = render.render_qr(url, tape, lines, max_length_mm=max_len,
                                       uppercase=uppercase, heights=heights)
        info["url"] = url
        return image, info

    def render_homebox(self, body, tape, max_len):
        base_url = _str(body, "homebox_url") or self.options["homebox_url"]
        if not base_url:
            raise BadRequest("homebox_url is not configured")
        asset_id = _str(body, "asset_id", limit=20)
        item_id = _str(body, "item_id", limit=40)
        if not asset_id and not item_id:
            raise BadRequest("'asset_id' or 'item_id' is required")
        name = _str(body, "name")
        location = _str(body, "location")
        if name is None or ("location" not in body and location is None):
            if self.homebox is None:
                if name is None:
                    raise BadRequest("'name' is required (or set homebox_api_key "
                                     "so it can be looked up)")
            else:
                item = self.homebox.lookup(asset_id=asset_id, item_id=item_id)
                name = name or item.get("name")
                if "location" not in body:
                    location = location or item.get("location")
                if not asset_id and item.get("asset_id"):
                    asset_id = item["asset_id"]
        uppercase = _bool(body, "uppercase", self.options["qr_uppercase"]) and \
            bool(asset_id)
        image, info = render.render_homebox(
            base_url, tape, asset_id=asset_id, item_id=item_id, name=name,
            location=location, max_length_mm=max_len, uppercase=uppercase)
        info["name"] = name
        info["location"] = location
        return image, info


# ---------------------------------------------------------------------------
# Jobs and the printer worker
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, kind, params):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.params = params
        self.state = "queued"       # queued|running|waiting|done|failed|cancelled
        self.error = None
        self.result = {}
        self.created = time.time()
        self.finished = None
        self.attempts = 0
        self.next_retry = None
        self.cancel_requested = threading.Event()
        self.done = threading.Event()

    @property
    def active(self):
        return self.state in ("queued", "running", "waiting")

    def as_dict(self):
        return {"id": self.id, "kind": self.kind, "state": self.state,
                "error": self.error, "result": self.result, "attempts": self.attempts,
                "next_retry": self.next_retry,
                "created": self.created, "finished": self.finished}


class PrinterWorker(threading.Thread):
    """Runs jobs one at a time. A job whose printer is unreachable waits and
    is retried every retry_interval_s for up to retry_minutes; the jobs
    behind it stay queued and follow as soon as the printer answers."""

    def __init__(self, options):
        super().__init__(daemon=True, name="printer")
        self.options = options
        self.queue = queue.Queue()
        self.jobs = {}
        self.order = []
        self.lock = threading.Lock()
        self.current = None
        self.last_status = None
        self.last_status_time = None
        self.last_error = None
        self.printed = 0
        verbose = options["log_level"] == "debug"
        self.logger = lw.Logger(verbose=verbose,
                                emit=lambda m: log.info("printer: %s", m) if m else None)
        # Retries log the connection attempts at debug level only.
        self.quiet_logger = lw.Logger(verbose=verbose,
                                      emit=lambda m: log.debug("printer: %s", m) if m else None)

    def submit(self, kind, params):
        job = Job(kind, params)
        with self.lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            while len(self.order) > KEEP_JOBS:
                old = self.jobs.get(self.order[0])
                if old and old.active:
                    break
                self.jobs.pop(self.order.pop(0), None)
        self.queue.put(job)
        log.info("queued job %s (%s)", job.id, kind)
        return job

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id=None):
        """Cancel one job (or all active ones). A job that is sending data to
        the printer right now cannot be cancelled. Returns cancelled ids."""
        with self.lock:
            jobs = [self.jobs[job_id]] if job_id in self.jobs else \
                ([] if job_id else list(self.jobs.values()))
        cancelled = []
        for job in jobs:
            if job.state in ("queued", "waiting"):
                job.cancel_requested.set()
                if job.state == "queued":
                    self._finish(job, "cancelled")
                cancelled.append(job.id)
        return cancelled

    def _finish(self, job, state, error=None):
        job.state = state
        if error is not None:
            job.error = error
        job.next_retry = None
        job.finished = time.time()
        job.done.set()

    def run(self):
        while True:
            job = self.queue.get()
            if job.cancel_requested.is_set():
                if job.state != "cancelled":
                    self._finish(job, "cancelled")
                continue
            self.current = job
            try:
                self.run_job(job)
            finally:
                self.current = None

    def run_job(self, job):
        retry_s = max(0, int(self.options["retry_minutes"])) * 60
        interval = max(5, int(self.options["retry_interval_s"]))
        deadline = time.time() + retry_s
        while True:
            job.state = "running"
            job.attempts += 1
            job.next_retry = None
            try:
                self.execute(job, quiet=job.attempts > 1)
            except lw.PrinterUnavailableError as exc:
                reason = str(exc) or exc.__class__.__name__
                self.last_error = f"printer not reachable: {reason}"
                if job.kind == "status" or time.time() + interval > deadline:
                    log.error("job %s failed: printer not reachable (%s)", job.id, reason)
                    self._finish(job, "failed", self.last_error)
                    return
                if job.attempts == 1:
                    log.info("job %s: printer not reachable (%s) - waiting, retrying "
                             "every %d s for up to %d min", job.id, reason, interval,
                             retry_s // 60)
                job.state = "waiting"
                job.error = self.last_error
                job.next_retry = time.time() + interval
                if job.cancel_requested.wait(interval):
                    log.info("job %s cancelled while waiting", job.id)
                    self._finish(job, "cancelled")
                    return
                continue
            except Exception as exc:  # report every other failure on the job
                error = str(exc) or exc.__class__.__name__
                self.last_error = error
                log.error("job %s failed: %s", job.id, error)
                self._finish(job, "failed", error)
                return
            self.last_error = None
            if job.attempts > 1:
                log.info("job %s done after %d attempts", job.id, job.attempts)
            job.error = None
            self._finish(job, "done")
            return

    def _remember(self, status):
        if status is not None:
            self.last_status = status
            self.last_status_time = time.time()

    def execute(self, job, quiet=False):
        mac = self.options["printer_mac"]
        if not mac and not SIMULATE:
            raise RuntimeError("printer_mac is not configured")
        logger = self.quiet_logger if quiet else self.logger
        if SIMULATE and SIMULATE_OFFLINE and os.path.exists(SIMULATE_OFFLINE):
            raise lw.PrinterUnavailableError("simulated: printer switched off")
        if job.kind == "print":
            bitmap, plan_options = job.params
            if SIMULATE:
                plan = lw.plan_bitmap(bitmap, **plan_options)
                job.result = {"simulated": True, "bytes": len(plan.stream_bytes())}
                log.info("simulated print: %s", job.result)
                self.printed += 1
                return
            options = dict(plan_options)            # keep params intact for retries
            tape = options.pop("tape_mm")
            _, status = lw.print_bitmap(bitmap, tape, mac=mac, logger=logger, **options)
            self._remember(status)
            self.printed += 1
        elif job.kind in ("feed", "cut"):
            if not SIMULATE:
                lw.feed_tape(mac=mac, cut=job.kind == "cut", logger=logger)
        elif job.kind == "status":
            if not SIMULATE:
                self._remember(lw.query_status(mac=mac, logger=logger))
        else:
            raise RuntimeError(f"unknown job kind {job.kind}")
        job.result = {"status": status_dict(self.last_status)}

    def snapshot(self):
        with self.lock:
            active = [j for j in self.jobs.values() if j.active]
        current = self.current
        waiting = current is not None and current.state == "waiting"
        return {
            "busy": current is not None and current.state == "running",
            "waiting_for_printer": waiting,
            "waiting_since": current.created if waiting else None,
            "pending": len(active),
            "queued": sum(1 for j in active if j.state == "queued"),
            "printed_since_start": self.printed,
            "last_error": self.last_error,
            "printer": status_dict(self.last_status),
            "printer_status_age_s": (round(time.time() - self.last_status_time)
                                     if self.last_status_time else None),
            "configured_tape_mm": self.options["tape_mm"],
            "simulate": SIMULATE,
        }


def status_dict(status):
    if status is None:
        return None
    return {
        "state": status.st_text, "st": status.st,
        "error": status.er_text if status.er else None, "er": status.er,
        "tape_mm": status.tape_width_mm, "tape_type": lw.tr_text(status.tr),
        "ready": status.is_ready,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_handler(options, worker, renderer):
    token = options["api_token"].strip()

    class Handler(BaseHTTPRequestHandler):
        server_version = "lwc410-print/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        # -- helpers --------------------------------------------------------
        def send_json(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_png(self, image, info):
            out = io.BytesIO()
            image.convert("1").save(out, "PNG")
            data = out.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            if info:
                safe = {k: v for k, v in info.items() if isinstance(v, (str, int, float))}
                self.send_header("X-Label-Info", json.dumps(safe, ensure_ascii=True))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if not token:
                return True
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else \
                self.headers.get("X-Api-Key", "")
            if not given:
                given = (parse_qs(urlsplit(self.path).query).get("key") or [""])[-1]
            if not given:
                # Homebox replaces the label-service URL's query string, so
                # its token goes in the path: /api/homebox/label/<token>
                path = urlsplit(self.path).path.rstrip("/")
                if path.startswith("/api/homebox/label/"):
                    given = path.rsplit("/", 1)[-1]
            return hmac.compare_digest(given.strip().encode(), token.encode())

        def read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise BadRequest("request body too large")
            return self.rfile.read(length) if length else b""

        def json_body(self, raw, query):
            body = {k: v[-1] for k, v in query.items() if k != "key"}
            if raw:
                try:
                    data = json.loads(raw)
                except ValueError:
                    raise BadRequest("body must be JSON")
                if not isinstance(data, dict):
                    raise BadRequest("JSON body must be an object")
                body.update(data)
            return body

        # -- routing --------------------------------------------------------
        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def route(self, method):
            url = urlsplit(self.path)
            path = url.path.rstrip("/")
            query = parse_qs(url.query)
            try:
                if path == "/api/health":
                    return self.send_json(200, {"ok": True})
                if not path.startswith("/api/"):
                    return self.send_json(404, {"error": "not found"})
                if not self.authorized():
                    return self.send_json(401, {"error": "missing or wrong API token"})
                raw = self.read_body() if method == "POST" else b""

                if method == "GET" and path == "/api/status":
                    return self.send_json(200, worker.snapshot())
                if method == "GET" and path.startswith("/api/jobs/"):
                    job = worker.get(path.rsplit("/", 1)[-1])
                    if not job:
                        return self.send_json(404, {"error": "unknown job"})
                    return self.send_json(200, job.as_dict())
                if method == "GET" and (path == "/api/homebox/label" or
                                        path.startswith("/api/homebox/label/")):
                    return self.preview("homebox-service", self.json_body(b"", query))
                if method == "GET" and path.startswith("/api/preview/"):
                    return self.preview(path.rsplit("/", 1)[-1], self.json_body(b"", query))
                if method != "POST":
                    return self.send_json(405, {"error": "use POST"})

                if path == "/api/queue/clear":
                    return self.send_json(200, {"cancelled": worker.cancel()})
                if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                    job_id = path.split("/")[3]
                    job = worker.get(job_id)
                    if not job:
                        return self.send_json(404, {"error": "unknown job"})
                    if not worker.cancel(job_id):
                        return self.send_json(409, {"error": f"job is {job.state}",
                                                    "state": job.state})
                    return self.send_json(200, {"cancelled": [job_id]})
                if path == "/api/status/refresh":
                    return self.enqueue("status", None, self.json_body(raw, query))
                if path in ("/api/feed", "/api/cut"):
                    return self.enqueue(path.rsplit("/", 1)[-1], None,
                                        self.json_body(raw, query))
                if path == "/api/print/image":
                    return self.print_image(raw, query)
                if path.startswith("/api/print/"):
                    kind = path.rsplit("/", 1)[-1]
                    return self.print_label(kind, self.json_body(raw, query))
                return self.send_json(404, {"error": "not found"})
            except (BadRequest, render.RenderError, homebox.HomeboxError) as exc:
                return self.send_json(400, {"error": str(exc)})
            except Exception as exc:  # never kill the server thread
                log.exception("request failed")
                return self.send_json(500, {"error": str(exc)})

        def preview(self, kind, body):
            image, info = renderer.render(kind, body, _tape(body, options))
            return self.send_png(image, info)

        def print_label(self, kind, body):
            plan_options = print_options(body, options)
            image, info = renderer.render(kind, body, plan_options["tape_mm"])
            if _bool(body, "preview"):
                return self.send_png(image, info)
            bitmap = render.to_bitmap(image)
            lw.plan_bitmap(bitmap, **plan_options)          # validate before queueing
            return self.enqueue("print", (bitmap, plan_options), body, info)

        def print_image(self, raw, query):
            body = {k: v[-1] for k, v in query.items() if k != "key"}
            if not raw:
                raise BadRequest("send the PNG/JPEG as the request body")
            try:
                image = Image.open(io.BytesIO(raw))
                image.load()
            except Exception:
                raise BadRequest("body is not a readable image")
            body["_image"] = image
            return self.print_label("image", body)

        def enqueue(self, kind, params, body, info=None):
            job = worker.submit(kind, params)
            if _bool(body, "wait"):
                job.done.wait(timeout=180)
                payload = job.as_dict()
                payload["label"] = {k: v for k, v in (info or {}).items()
                                    if isinstance(v, (str, int, float))}
                code = 200 if job.state == "done" else 202 if job.active else 502
                return self.send_json(code, payload)
            return self.send_json(202, {"id": job.id, "state": job.state,
                                        "label": {k: v for k, v in (info or {}).items()
                                                  if isinstance(v, (str, int, float))}})

    return Handler


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

def check_bluetooth():
    """Log clear hints if this host/container cannot do Bluetooth Classic."""
    if SIMULATE:
        return
    if not hasattr(socket, "AF_BLUETOOTH"):
        log.error("Python has no AF_BLUETOOTH support - printing will fail")
        return
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.close()
        log.info("RFCOMM sockets available")
    except OSError as exc:
        log.error("cannot create an RFCOMM socket (%s). Needs host_network and a kernel "
                  "with Bluetooth RFCOMM support; see the add-on documentation.", exc)
    try:
        out = subprocess.run(["bluetoothctl", "power", "on"], capture_output=True,
                             text=True, timeout=15)
        log.info("bluetoothctl power on: %s", (out.stdout or out.stderr).strip()[-200:])
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not power on the adapter via bluetoothctl: %s", exc)


def main():
    options = load_options()
    logging.basicConfig(
        level=getattr(logging, str(options["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    log.info("LW-C410 print service: printer %s, %s mm tape, Homebox %s%s",
             options["printer_mac"] or "(not set)", options["tape_mm"],
             options["homebox_url"] or "(not set)", " [SIMULATE]" if SIMULATE else "")
    if not options["api_token"]:
        log.warning("api_token is empty: anyone on your network can print")
    check_bluetooth()

    worker = PrinterWorker(options)
    worker.start()
    renderer = Renderer(options)
    server = ThreadingHTTPServer(("0.0.0.0", options["port"]),
                                 make_handler(options, worker, renderer))
    server.daemon_threads = True
    log.info("listening on port %d", options["port"])
    server.serve_forever()


if __name__ == "__main__":
    main()
