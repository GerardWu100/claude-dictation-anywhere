"""The settings server on a real loopback socket: origin checks, errors, and cleanup."""

import http.client
import json
import os
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from claude_dictation_anywhere import web_ui


class ServerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.controller = Mock()
        self.controller.status.return_value = {"state": "idle"}
        self.page = web_ui.SettingsPage(self.controller)
        self.page.start()
        self.addCleanup(self.page.stop)
        self.port = self.page.server.server_address[1]

    def request(self, method, path, host=None, origin=None, token=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin:
            headers["Origin"] = origin
        if token:
            headers["X-CDA-Token"] = token
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response, response.read()


class HostCheckTest(ServerCase):
    def test_loopback_names_get_the_page_with_its_token(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            with self.subTest(host):
                response, body = self.request("GET", "/", host=host)
                self.assertEqual(response.status, 200)
                self.assertIn(self.page.token.encode(), body)

    def test_other_host_names_get_no_token_and_no_data(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", "127.0.0.1:1"):
            with self.subTest(host):
                response, body = self.request("GET", "/", host=host)
                self.assertEqual(response.status, 403)
                self.assertNotIn(self.page.token.encode(), body)
                response, body = self.request(
                    "GET", "/api/status", host=host, token=self.page.token
                )
                self.assertEqual(response.status, 403)
                self.assertNotIn(b"idle", body)

    def test_hostile_origin_cannot_change_anything_even_with_the_token(self):
        response, _ = self.request(
            "POST",
            "/api/settings",
            origin="http://evil.example",
            token=self.page.token,
            body=b'{"mode":"toggle"}',
        )
        self.assertEqual(response.status, 403)
        self.controller.update_settings.assert_not_called()

    def test_own_origin_can_change_settings(self):
        response, _ = self.request(
            "POST",
            "/api/settings",
            origin=f"http://127.0.0.1:{self.port}",
            token=self.page.token,
            body=b'{"mode":"toggle"}',
        )
        self.assertEqual(response.status, 200)
        self.controller.update_settings.assert_called_once_with({"mode": "toggle"})

    def test_wrong_token_is_refused_and_named_stale(self):
        response, body = self.request("GET", "/api/status", token="from-an-earlier-run")
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(body)["code"], "stale_token")

    def test_non_ascii_token_is_refused_not_a_crash(self):
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(connection.close)
        connection.sendall(
            f"GET /api/status HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\n".encode()
            + "X-CDA-Token: é\r\n\r\n".encode("latin-1")
        )
        self.assertIn(b"403", connection.recv(100))

    def test_security_headers_are_on_every_reply(self):
        for path in ("/", "/nothing"):
            response, _ = self.request("GET", path)
            self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
            self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")


class ErrorRepliesTest(ServerCase):
    def post(self, path, body):
        return self.request("POST", path, token=self.page.token, body=body)

    def test_bad_json_and_bad_shapes_are_400(self):
        for path, body in (
            ("/api/settings", b"{not json"),
            ("/api/recovery/clear", b"[]"),
            ("/api/open-privacy", b"[]"),
        ):
            with self.subTest(path):
                response, reply = self.post(path, body)
                self.assertEqual(response.status, 400)
                self.assertIn("error", json.loads(reply))

    def test_invalid_value_is_400_with_its_message(self):
        self.controller.update_settings.side_effect = ValueError("mode must be hold or toggle")
        response, reply = self.post("/api/settings", b'{"mode":"x"}')
        self.assertEqual(response.status, 400)
        self.assertIn("mode must be", json.loads(reply)["error"])

    def test_failed_save_is_a_structured_500_without_details(self):
        self.controller.update_settings.side_effect = PermissionError(
            13, "Permission denied", "/home/secret/settings.toml"
        )
        response, reply = self.post("/api/settings", b'{"mode":"toggle"}')
        self.assertEqual(response.status, 500)
        self.assertIn("Permission denied", json.loads(reply)["error"])
        self.assertNotIn(b"secret", reply)
        self.assertNotIn(b"Traceback", reply)

    def test_unexpected_failure_is_a_structured_500_not_a_dropped_connection(self):
        self.controller.update_settings.side_effect = RuntimeError("secret detail")
        with self.assertLogs("claude_dictation_anywhere.web_ui", "ERROR"):
            response, reply = self.post("/api/settings", b'{"mode":"toggle"}')
        self.assertEqual(response.status, 500)
        self.assertNotIn(b"secret", reply)

    def test_recovery_clear_reaches_the_controller(self):
        response, _ = self.post("/api/recovery/clear", b'{"id": 3}')
        self.assertEqual(response.status, 200)
        self.controller.clear_recovery.assert_called_once_with(3)

    def test_oversized_body_is_refused_without_reading_it(self):
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(connection.close)
        connection.sendall(
            f"POST /api/settings HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"X-CDA-Token: {self.page.token}\r\n"
            f"Content-Length: {web_ui.MAX_BODY_BYTES + 1}\r\n\r\n".encode()
        )
        self.assertIn(b"400", connection.recv(100))
        self.controller.update_settings.assert_not_called()


class StalledRequestTest(unittest.TestCase):
    def test_stalled_client_is_dropped(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}),
            patch.object(web_ui, "REQUEST_TIMEOUT_S", 0.2),
        ):
            page = web_ui.SettingsPage(Mock())
            page.start()
            self.addCleanup(page.stop)
            connection = socket.create_connection(
                ("127.0.0.1", page.server.server_address[1]), timeout=5
            )
            self.addCleanup(connection.close)
            connection.sendall(
                f"POST /api/settings HTTP/1.0\r\nHost: 127.0.0.1:{page.server.server_address[1]}"
                f"\r\nX-CDA-Token: {page.token}\r\nContent-Length: 100\r\n\r\n".encode()
            )  # and then nothing: the promised body never comes
            began = time.monotonic()
            self.assertIn(b"408", connection.recv(100))
            self.assertLess(time.monotonic() - began, 3)
            page.controller.update_settings.assert_not_called()


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_stop_frees_the_port_and_can_repeat(self):
        page = web_ui.SettingsPage(Mock())
        page.start()
        port = page.server.server_address[1]
        address_file = web_ui.running_page_url_path()
        self.assertEqual(oct(address_file.stat().st_mode & 0o777), "0o600")
        page.stop()
        page.stop()
        self.assertFalse(address_file.exists())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        rebound = socket.socket()
        self.addCleanup(rebound.close)
        rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rebound.bind(("127.0.0.1", port))

    def test_stop_without_start_does_not_hang(self):
        page = web_ui.SettingsPage(Mock())
        page.stop()
        page.stop()

    def test_page_has_a_token_placeholder_filled_in(self):
        page = web_ui.SettingsPage(Mock())
        self.addCleanup(page.stop)
        self.assertNotIn(b"__CDA_TOKEN__", page.page)


if __name__ == "__main__":
    unittest.main()
