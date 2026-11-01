"""Delivery: deadlines, the clipboard promise, and never typing twice. Helpers are real or fake programs."""

import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from claude_dictation_anywhere import output, settings


def options(method="type", copy=True):
    return dict(settings.DEFAULT_SETTINGS, output=method, copy_to_clipboard=copy)


class RunHelperTest(unittest.TestCase):
    def test_hanging_helper_is_killed_at_its_deadline(self):
        began = time.monotonic()
        with self.assertRaises(output.HelperTimeout):
            output.run_helper(["sleep", "30"], None, 0.1)
        self.assertLess(time.monotonic() - began, 2)

    def test_cancel_kills_it_promptly(self):
        cancel = threading.Event()
        threading.Timer(0.1, cancel.set).start()
        began = time.monotonic()
        with self.assertRaises(output.HelperCancelled):
            output.run_helper(["sleep", "30"], None, 30, cancel)
        self.assertLess(time.monotonic() - began, 2)

    def test_failed_helper_raises(self):
        with self.assertRaises(subprocess.CalledProcessError):
            output.run_helper(["false"], None, 5)

    def test_missing_helper_raises_oserror(self):
        with self.assertRaises(OSError):
            output.run_helper(["no-such-program-here"], None, 5)

    def test_input_reaches_the_helper_even_when_it_is_slow_to_read(self):
        with self.assertRaises(subprocess.CalledProcessError):
            output.run_helper(["sh", "-c", 'sleep 0.2; [ "$(cat)" = hi ] && exit 3'], b"hi", 5)
        output.run_helper(["sh", "-c", 'sleep 0.2; [ "$(cat)" = hi ]'], b"hi", 5)

    def test_large_input_does_not_deadlock(self):
        output.run_helper(["cat"], b"x" * 1_000_000, 5)


class DeliverTest(unittest.TestCase):
    def deliver(self, *args, front=None, copy_ok=True, insert_error=None, **kwargs):
        with (
            patch.object(output, "focused_app", return_value=front),
            patch.object(output, "copy_to_clipboard", return_value=copy_ok) as copy,
            patch.object(output, "insert_text", side_effect=insert_error) as insert,
        ):
            result = output.deliver("hello", *args, **kwargs)
        return result, copy, insert

    def test_type_and_copy(self):
        result, copy, insert = self.deliver(options())
        self.assertTrue(result.copied and result.inserted)
        self.assertEqual(result.problem, "")
        copy.assert_called_once()
        insert.assert_called_once()

    def test_clipboard_off_never_writes_the_clipboard_even_after_a_focus_change(self):
        result, copy, insert = self.deliver(options(copy=False), target_app=111, front=222)
        copy.assert_not_called()
        insert.assert_not_called()
        self.assertFalse(result.inserted or result.copied)
        self.assertIn("switched apps", result.problem)
        self.assertNotIn("clipboard", result.problem)

    def test_clipboard_on_copies_and_a_focus_change_only_skips_typing(self):
        result, copy, insert = self.deliver(options(), target_app=111, front=222)
        copy.assert_called_once()
        insert.assert_not_called()
        self.assertTrue(result.copied)

    def test_same_app_is_typed(self):
        result, _, insert = self.deliver(options(), target_app=111, front=111)
        insert.assert_called_once()
        self.assertTrue(result.inserted)

    def test_none_mode_with_copy_off_does_nothing(self):
        result, copy, insert = self.deliver(options("none", copy=False))
        copy.assert_not_called()
        insert.assert_not_called()
        self.assertEqual(result.problem, "")

    def test_none_mode_copies(self):
        result, copy, _ = self.deliver(options("none"))
        copy.assert_called_once()
        self.assertTrue(result.copied)

    def test_paste_uses_the_clipboard_through_insertion_only(self):
        result, copy, insert = self.deliver(options("paste", copy=False))
        copy.assert_not_called()  # no second, separate copy
        insert.assert_called_once()
        self.assertTrue(result.copied)

    def test_copy_failure_is_reported_apart_and_typing_goes_on(self):
        with (
            patch.object(output, "copy_to_clipboard", side_effect=OSError("gone")),
            patch.object(output, "insert_text") as insert,
            patch.object(output, "focused_app", return_value=None),
        ):
            result = output.deliver("hello", options())
        insert.assert_called_once()
        self.assertTrue(result.inserted)
        self.assertFalse(result.copied)
        self.assertIn("clipboard", result.problem)

    def test_no_clipboard_tool_does_not_claim_a_copy(self):
        result, _, _ = self.deliver(options(), copy_ok=False)
        self.assertFalse(result.copied)
        self.assertIn("not copied", result.problem)

    def test_uncertain_failure_is_marked_and_only_one_attempt_is_made(self):
        result, _, insert = self.deliver(
            options(), insert_error=output.InsertionFailed("typing did not finish", uncertain=True)
        )
        insert.assert_called_once()
        self.assertTrue(result.uncertain)
        self.assertFalse(result.inserted)
        self.assertIn("may be in the app", result.problem)

    def test_failure_before_any_text_is_not_uncertain(self):
        result, _, _ = self.deliver(
            options(), insert_error=output.InsertionFailed("no ydotool", uncertain=False)
        )
        self.assertFalse(result.uncertain)

    def test_cancel_before_typing_types_nothing(self):
        cancel = threading.Event()
        cancel.set()
        with (
            patch.object(output, "copy_to_clipboard", side_effect=output.HelperCancelled("x")),
            patch.object(output, "insert_text") as insert,
            patch.object(output, "focused_app", return_value=None),
        ):
            result = output.deliver("hello", options(), None, cancel)
        insert.assert_not_called()
        self.assertTrue(result.problem)


class LinuxInsertTest(unittest.TestCase):
    """The Linux code paths with a fake helper runner; they contain no Linux-only calls."""

    def setUp(self):
        self.commands = []
        self.stdin = []
        self.fail_on = None
        self.error = subprocess.CalledProcessError(1, "x")

        def run(args, stdin, timeout_s, cancel=None):
            self.commands.append(args)
            self.stdin.append(stdin)
            self.timeout = timeout_s
            if self.fail_on and self.fail_on in args:
                raise self.error

        patches = [
            patch.object(output, "IS_MAC", False),
            patch.object(output, "run_helper", run),
            patch.object(output, "clipboard_command", return_value=["wl-copy"]),
            patch.object(output, "WAKE_BEFORE_TYPING_S", 0),
            patch.object(output, "PASTE_AFTER_COPY_S", 0),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_typing_has_no_enter_and_a_deadline_that_grows_with_the_text(self):
        output.insert_text("one\ntwo\r\nthree “q”", "type")
        typed = self.stdin[-1].decode()
        self.assertEqual(typed, 'one two three "q"')
        self.assertNotIn("\n", typed)
        short = self.timeout
        output.insert_text("x" * 5000, "type")
        self.assertGreater(self.timeout, short + 50)

    def test_text_never_appears_in_command_arguments(self):
        output.insert_text("private words", "type")
        output.insert_text("private words", "paste")
        for command in self.commands:
            self.assertFalse(any("private" in part for part in command))

    def test_typing_failure_after_it_began_is_uncertain(self):
        self.fail_on = "type"
        with self.assertRaises(output.InsertionFailed) as caught:
            output.insert_text("hi", "type")
        self.assertTrue(caught.exception.uncertain)

    def test_typing_timeout_is_uncertain(self):
        self.fail_on, self.error = "type", output.HelperTimeout("ydotool")
        with self.assertRaises(output.InsertionFailed) as caught:
            output.insert_text("hi", "type")
        self.assertTrue(caught.exception.uncertain)

    def test_wake_key_failure_is_not_uncertain(self):
        self.fail_on, self.error = "key", OSError("no ydotool")
        with self.assertRaises(output.InsertionFailed) as caught:
            output.insert_text("hi", "type")
        self.assertFalse(caught.exception.uncertain)

    def test_paste_without_a_clipboard_tool_is_a_clear_failure(self):
        with (
            patch.object(output, "clipboard_command", return_value=None),
            self.assertRaisesRegex(output.InsertionFailed, "wl-clipboard"),
        ):
            output.insert_text("hi", "paste")

    def test_paste_keeps_line_breaks(self):
        output.insert_text("one\ntwo", "paste")
        self.assertEqual(self.stdin[0], b"one\ntwo")


class LineBreakTest(unittest.TestCase):
    def test_typed_text_has_no_line_breaks(self):
        self.assertEqual(output.without_line_breaks("a\nb\r\nc"), "a b c")

    def test_make_typeable(self):
        self.assertEqual(output.make_typeable("“It’s—fine…” "), '"It\'s-fine..." ')


if __name__ == "__main__":
    unittest.main()
