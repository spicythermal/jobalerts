"""A tiny read-only web server for the cost-of-living map, so a Home Assistant Webpage card can show it.

It serves one page from memory (`/`, also `/map`) plus `/healthz`; the runner swaps in a new page
whenever the dashboard refreshes. Standard library only, in a background thread.
"""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

PLACEHOLDER = ("<!doctype html><meta charset=utf-8><title>Cost-of-living map</title>"
               "<p style='font-family:system-ui'>The map is being built. Reload in a moment.</p>")


class MapServer:
    def __init__(self, bind: str = "0.0.0.0", port: int = 8765):
        self.bind, self.port = bind, port
        self._page = PLACEHOLDER.encode()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def update(self, html: str) -> None:
        self._page = html.encode("utf-8")  # one reference swap: readers see the old page or the new one

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2] if self._httpd else (self.bind, self.port)

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, body: bytes | None) -> None:
                if self.path.split("?")[0] in ("/", "/map", "/map/"):
                    status, ctype, payload = 200, "text/html; charset=utf-8", server._page
                elif self.path == "/healthz":
                    status, ctype, payload = 200, "text/plain; charset=utf-8", b"ok\n"
                else:
                    status, ctype, payload = 404, "text/plain; charset=utf-8", b"not found\n"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if body is None:
                    self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
                self._send(None)

            def do_HEAD(self) -> None:  # noqa: N802
                self._send(b"")

            def log_message(self, fmt: str, *args) -> None:
                log.debug("map %s - %s", self.address_string(), fmt % args)

        self._httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="map-server", daemon=True)
        self._thread.start()
        log.info("Cost-of-living map on http://%s:%d/", *self.address)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
