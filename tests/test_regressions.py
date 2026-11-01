"""Regression tests without Claude Code or desktop input."""

import io
import json
import unittest
from unittest.mock import Mock

from claude_dictation_anywhere import settings, web_ui


class RequestValidationTest(unittest.TestCase):
    def handler(self, length, body=b"{}"):
        page = object.__new__(web_ui.SettingsPage)
        page.token = "test-token"
        page.server = Mock(server_address=("127.0.0.1", 47821))
        page.controller = Mock()
        page.controller.update_settings.side_effect = settings.check_settings
        handler = object.__new__(page._handler())
        handler.headers = {
            "Host": "127.0.0.1:47821",
            "X-CDA-Token": page.token,
            "Content-Length": length,
        }
        handler.path = "/api/settings"
        handler.rfile = io.BytesIO(body)
        handler._json = Mock()
        return page, handler

    def test_open_privacy_rejects_unknown_permission(self):
        from claude_dictation_anywhere import controller as controller_module

        for body in (b'{"permission": "Camera"}', b"[]"):
            with self.subTest(body=body):
                page, handler = self.handler(str(len(body)), body)
                handler.path = "/api/open-privacy"
                page.controller.open_privacy_settings.side_effect = lambda permission: (
                    controller_module.DictationController.open_privacy_settings(Mock(), permission)
                )
                handler.do_POST()
                self.assertEqual(handler._json.call_args.args[1], 400)

    def test_bad_lengths_rejected_before_read_or_mutation(self):
        for length in ("-1", "invalid", str(web_ui.MAX_BODY_BYTES + 1)):
            with self.subTest(length=length):
                page, handler = self.handler(length)
                handler.rfile = Mock()
                handler.do_POST()
                handler.rfile.read.assert_not_called()
                page.controller.update_settings.assert_not_called()
                self.assertEqual(handler._json.call_args.args[1], 400)

    def test_non_object_settings_returns_bad_request(self):
        for value in ([], None, "text", 1):
            with self.subTest(value=value):
                body = json.dumps(value).encode()
                page, handler = self.handler(str(len(body)), body)
                handler.do_POST()
                self.assertEqual(handler._json.call_args.args[1], 400)
                page.controller.status.assert_not_called()

    def test_valid_settings_still_accepted(self):
        body = b'{"mode":"toggle"}'
        page, handler = self.handler(str(len(body)), body)
        handler.do_POST()
        page.controller.update_settings.assert_called_once_with({"mode": "toggle"})
        handler._json.assert_called_once_with(page.controller.status.return_value)
