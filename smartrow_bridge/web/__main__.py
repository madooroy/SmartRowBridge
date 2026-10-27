"""Optional web page for the bridge: what the screen shows, on any phone or tablet on your Wi-Fi.

    python -m smartrow_bridge.web            serve on SRB_WEB_PORT (8080), the smartrow-web service
    python -m smartrow_bridge.web --port 9000

Like the screen, it only reads the bridge's status file, so the bridge runs the same
with or without it. It serves, read-only: the page (index.html, next to this file),
its app manifest, icons and service worker, and /status.json, the bridge's status
file plus its age in seconds as the Pi sees it (so a tablet whose clock differs from
the Pi's can still tell when the bridge has stopped). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import status as status_file

log = logging.getLogger("smartrow_bridge.web")

HERE = Path(__file__).parent
PAGE = HERE / "index.html"
# Fixed files next to the page: route -> (file, content type). Nothing else is ever served.
FILES = {
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
MANIFEST = {
    "name": "SmartRow coach",
    "short_name": "Row coach",
    "display": "standalone",
    "background_color": "#000000",
    "theme_color": "#000000",
    "start_url": "/",
    "scope": "/",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    ],
}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def status_payload(path: str, now: float | None = None) -> dict:
    """The status file with "age_s" added, or a stand-in saying the bridge isn't writing it."""
    now = time.time() if now is None else now
    status = status_file.read(path)
    if status is None:
        return {"stage": None, "age_s": None}
    status["age_s"] = round(now - float(status.get("time", 0)), 1)
    return status


def make_handler(path: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "smartrow-web"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif route == "/status.json":
                self._send(HTTPStatus.OK, json.dumps(status_payload(path)).encode(), "application/json")
            elif route in FILES:
                name, content_type = FILES[route]
                self._send(HTTPStatus.OK, (HERE / name).read_bytes(), content_type)
            elif route == "/manifest.webmanifest":
                self._send(HTTPStatus.OK, json.dumps(MANIFEST).encode(), "application/manifest+json")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def log_message(self, fmt: str, *args) -> None:  # one request a second: keep the journal quiet
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def serve(port: int, path: str, host: str = "") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(path))
    server.daemon_threads = True
    return server


def main() -> None:
    p = argparse.ArgumentParser(prog="smartrow-web", description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=int(_env("SRB_WEB_PORT", "8080")))
    p.add_argument("--status-file", default=_env("SRB_STATUS_FILE", status_file.DEFAULT_PATH))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    server = serve(args.port, args.status_file)
    log.info("Web page on port %d; status file %s", args.port, args.status_file)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
