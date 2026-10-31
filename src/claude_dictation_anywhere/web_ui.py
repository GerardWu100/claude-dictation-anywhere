"""Settings and history page, served on this computer only (127.0.0.1) and opened in your browser.

Routes (all JSON except the page itself):

    GET  /                    the page (index.html, with this run's token filled in)
    GET  /api/status          controller.status()
    POST /api/settings        body: {setting: value, ...}; replies with the new status
    POST /api/capture         start waiting for a new shortcut
    POST /api/history/clear   delete the saved history

Every /api request must carry the header ``X-CDA-Token`` with a random token
that is only written into the page. Other web pages you visit cannot read the
page, so they cannot learn the token or change settings behind your back.
POST bodies must fit within MAX_BODY_BYTES; invalid lengths and non-object
settings payloads receive HTTP 400 without applying settings.
"""

import json
import logging
import secrets
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .settings import IS_MAC, runtime_dir

log = logging.getLogger(__name__)

PREFERRED_PORT = 47821  # fixed so a bookmark keeps working; any free port if taken
HOST = "127.0.0.1"
MAX_BODY_BYTES = 64_000


def running_page_url_path():
    """Where the running copy writes its page address, so a second launch can open it."""
    return runtime_dir() / "settings_url"


def open_in_browser(url: str) -> None:
    """Open with the desktop's default browser (xdg-open follows the GNOME/KDE setting)."""
    subprocess.Popen(
        ["open" if IS_MAC else "xdg-open", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class SettingsPage:
    def __init__(self, controller):
        self.controller = controller
        self.token = secrets.token_urlsafe(24)
        page = resources.files(__package__).joinpath("web/index.html").read_text(encoding="utf-8")
        self.page = page.replace("__CDA_TOKEN__", self.token).encode()
        try:
            self.server = ThreadingHTTPServer((HOST, PREFERRED_PORT), self._handler())
        except OSError:
            self.server = ThreadingHTTPServer((HOST, 0), self._handler())

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.server.server_address[1]}/"

    def start(self) -> None:
        threading.Thread(
            target=self.server.serve_forever, daemon=True, name="settings-page"
        ).start()
        running_page_url_path().write_text(self.url, encoding="utf-8")
        log.info("settings page at %s", self.url)

    def open(self) -> None:
        open_in_browser(self.url)

    def stop(self) -> None:
        self.server.shutdown()
        running_page_url_path().unlink(missing_ok=True)

    def _handler(self):
        page = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # keep request noise out of the log
                pass

            def _reply(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
                self._reply(status, json.dumps(data).encode(), "application/json")

            def _authorised(self) -> bool:
                if secrets.compare_digest(self.headers.get("X-CDA-Token", ""), page.token):
                    return True
                self._json({"error": "missing or wrong token"}, HTTPStatus.FORBIDDEN)
                return False

            def do_GET(self):
                if self.path == "/":
                    self._reply(HTTPStatus.OK, page.page, "text/html; charset=utf-8")
                elif self.path == "/api/status":
                    if self._authorised():
                        self._json(page.controller.status())
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

            def do_POST(self):
                if not self._authorised():
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if not 0 <= length <= MAX_BODY_BYTES:
                        raise ValueError(f"Content-Length must be 0-{MAX_BODY_BYTES}")
                    body = self.rfile.read(length) if length else b"{}"
                    data = json.loads(body or b"{}")
                    if self.path == "/api/settings":
                        page.controller.update_settings(data)
                    elif self.path == "/api/capture":
                        page.controller.capture_shortcut()
                    elif self.path == "/api/history/clear":
                        page.controller.history.clear()
                    else:
                        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                        return
                except (ValueError, json.JSONDecodeError) as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(page.controller.status())

        return Handler
