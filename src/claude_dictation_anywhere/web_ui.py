"""Settings and history page, served on this computer only (127.0.0.1) and opened in your browser.

Routes (all JSON except the page itself):

    GET  /                    the page (index.html, with this run's token filled in)
    GET  /api/status          controller.status()
    POST /api/settings        body: {setting: value, ...}; replies with the new status
    POST /api/capture         start waiting for a new shortcut
    POST /api/history/clear   delete the saved history
    POST /api/recovery/clear  body {"id": n}: forget one transcript kept for recovery
    POST /api/open-privacy    macOS, body {"permission": "Accessibility"}: open its System Settings page

Every request must name this server in its ``Host`` header, before the page or
any data is returned: a web page that points its own domain name at 127.0.0.1
(DNS rebinding) sends a different Host, so it never sees the token. A POST that
carries an ``Origin`` must come from this server too. Every /api request must
carry the header ``X-CDA-Token`` with a random token that is only written into
the page; an old tab from an earlier run gets ``code: "stale_token"``.

POST bodies must fit within MAX_BODY_BYTES; invalid lengths and non-object
settings payloads receive HTTP 400 without applying settings. A failed save or
other runtime failure receives HTTP 500 with a short message, never a traceback.
A client that stalls is dropped after REQUEST_TIMEOUT_S.
"""

import json
import logging
import secrets
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .settings import IS_MAC, runtime_dir, write_private_file

log = logging.getLogger(__name__)

PREFERRED_PORT = 47821  # fixed so a bookmark keeps working; any free port if taken
HOST = "127.0.0.1"
MAX_BODY_BYTES = 64_000
REQUEST_TIMEOUT_S = 5
SERVER_STOP_TIMEOUT_S = 2
# The page is one self-contained file; nothing else may load, frame it, or receive its address.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


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
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.server.server_address[1]}/"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="settings-page"
        )
        self._thread.start()
        write_private_file(running_page_url_path(), self.url)
        log.info("settings page at %s", self.url)

    def open(self) -> None:
        open_in_browser(self.url)

    def stop(self) -> None:
        """Stop serving and free the port; safe to repeat, and after a failed start."""
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:  # shutdown() would wait forever on a server never started
            self.server.shutdown()
            self._thread.join(SERVER_STOP_TIMEOUT_S)
        self.server.server_close()
        try:
            running_page_url_path().unlink(missing_ok=True)
        except OSError as error:
            log.warning("could not remove the page address: %s", error.strerror)

    def _allowed_authorities(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"{HOST}:{port}", f"localhost:{port}"}

    def _handler(self):
        page = self

        class Handler(BaseHTTPRequestHandler):
            timeout = REQUEST_TIMEOUT_S

            def log_message(self, *_args):  # keep request noise out of the log
                pass

            def _reply(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
                self._reply(status, json.dumps(data).encode(), "application/json")

            def _host_allowed(self) -> bool:
                """Refuse a request addressed to any other name before anything is disclosed."""
                if self.headers.get("Host", "") in page._allowed_authorities():
                    return True
                self._json({"error": "unexpected Host"}, HTTPStatus.FORBIDDEN)
                return False

            def _origin_allowed(self) -> bool:
                origin = self.headers.get("Origin")
                allowed = {f"http://{a}" for a in page._allowed_authorities()}
                if origin is None or origin in allowed:
                    return True
                self._json({"error": "unexpected Origin"}, HTTPStatus.FORBIDDEN)
                return False

            def _authorised(self) -> bool:
                sent = self.headers.get("X-CDA-Token", "").encode()
                if secrets.compare_digest(sent, page.token.encode()):
                    return True
                self._json(
                    {"error": "missing or wrong token", "code": "stale_token"},
                    HTTPStatus.FORBIDDEN,
                )
                return False

            def do_GET(self):
                if not self._host_allowed():
                    return
                if self.path == "/":
                    self._reply(HTTPStatus.OK, page.page, "text/html; charset=utf-8")
                elif self.path == "/api/status":
                    if self._authorised():
                        self._json(page.controller.status())
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

            def do_POST(self):
                if not (self._host_allowed() and self._origin_allowed() and self._authorised()):
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
                    elif self.path == "/api/recovery/clear":
                        if not isinstance(data, dict):
                            raise ValueError("body must be a JSON object")
                        page.controller.clear_recovery(data.get("id"))
                    elif self.path == "/api/open-privacy":
                        if not isinstance(data, dict):
                            raise ValueError("body must be a JSON object")
                        page.controller.open_privacy_settings(data.get("permission"))
                    else:
                        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                        return
                except (ValueError, json.JSONDecodeError) as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                except TimeoutError:  # the promised body never came
                    self._json({"error": "request timed out"}, HTTPStatus.REQUEST_TIMEOUT)
                    return
                except OSError as error:
                    log.warning("request %s failed: %s", self.path, error.strerror)
                    self._json(
                        {"error": f"Could not save: {error.strerror or 'disk error'}."},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                except Exception:
                    log.exception("request %s failed", self.path)
                    self._json(
                        {"error": "Something went wrong in this app; the log has details."},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._json(page.controller.status())

        return Handler
