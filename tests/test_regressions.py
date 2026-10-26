"""Regression tests without Claude Code or desktop input."""

import io
import json
import unittest
from unittest.mock import Mock, patch

from claude_dictation_anywhere import claude_session, settings, web_ui


class SessionLifecycleTest(unittest.TestCase):
    def test_reaped_child_closes_terminal_once(self):
        session = object.__new__(claude_session.ClaudeVoiceSession)
        session.pid, session.fd = 123, 456
        with (
            patch.object(claude_session.os, "waitpid", return_value=(123, 0)),
            patch.object(claude_session.os, "close") as close,
        ):
            self.assertFalse(session.is_alive())
            self.assertFalse(session.is_alive())
            session.stop()
        close.assert_called_once_with(456)
        self.assertIsNone(session.fd)
        self.assertIsNone(session.pid)

    def test_already_reaped_child_closes_terminal(self):
        session = object.__new__(claude_session.ClaudeVoiceSession)
        session.pid, session.fd = 123, 456
        with (
            patch.object(claude_session.os, "waitpid", side_effect=ChildProcessError),
            patch.object(claude_session.os, "close") as close,
        ):
            self.assertFalse(session.is_alive())
        close.assert_called_once_with(456)


class RequestValidationTest(unittest.TestCase):
    def handler(self, length, body=b"{}"):
        page = object.__new__(web_ui.SettingsPage)
        page.token = "test-token"
        page.controller = Mock()
        page.controller.update_settings.side_effect = settings.check_settings
        handler = object.__new__(page._handler())
        handler.headers = {"X-CDA-Token": page.token, "Content-Length": length}
        handler.path = "/api/settings"
        handler.rfile = io.BytesIO(body)
        handler._json = Mock()
        return page, handler

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
