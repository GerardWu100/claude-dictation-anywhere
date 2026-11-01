"""Check macOS shortcut parsing, naming, and the app-switch guard with synthetic Quartz events."""

import threading
import time
import unittest
from unittest.mock import Mock, patch

from claude_dictation_anywhere import settings

if settings.IS_MAC:
    import Quartz

    from claude_dictation_anywhere import mac_events, output

COMMAND = 0x100000  # kCGEventFlagMaskCommand
RIGHT_COMMAND_BIT, LEFT_COMMAND_BIT = 0x10, 0x08


def _as_hardware(event):
    """Events made in this process carry its ID, which the parser drops as its own typing."""
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID, 0)
    return event


def modifier_event(key_code: int, flags: int):
    event = Quartz.CGEventCreateKeyboardEvent(None, key_code, True)
    Quartz.CGEventSetType(event, Quartz.kCGEventFlagsChanged)
    Quartz.CGEventSetFlags(event, flags)
    return _as_hardware(event)


@unittest.skipUnless(settings.IS_MAC, "macOS events")
class MacEventParsingTest(unittest.TestCase):
    def test_right_command_down_and_up_while_left_is_held(self):
        changed = Quartz.kCGEventFlagsChanged
        down = modifier_event(54, COMMAND | RIGHT_COMMAND_BIT | LEFT_COMMAND_BIT)
        self.assertEqual(mac_events.event_input(changed, down), ("key:54", True, False))
        up = modifier_event(54, COMMAND | LEFT_COMMAND_BIT)
        self.assertEqual(mac_events.event_input(changed, up), ("key:54", False, False))

    def test_side_mouse_button_keeps_its_number(self):
        for event_type, pressed in (
            (Quartz.kCGEventOtherMouseDown, True),
            (Quartz.kCGEventOtherMouseUp, False),
        ):
            event = _as_hardware(Quartz.CGEventCreateMouseEvent(None, event_type, (0, 0), 3))
            self.assertEqual(mac_events.event_input(event_type, event), ("mouse:3", pressed, False))

    def test_auto_repeat_is_flagged_and_own_events_are_dropped(self):
        repeat = _as_hardware(Quartz.CGEventCreateKeyboardEvent(None, 100, True))
        Quartz.CGEventSetIntegerValueField(repeat, Quartz.kCGKeyboardEventAutorepeat, 1)
        self.assertEqual(
            mac_events.event_input(Quartz.kCGEventKeyDown, repeat), ("key:100", True, True)
        )
        own = Quartz.CGEventCreateKeyboardEvent(None, 100, True)
        self.assertIsNone(mac_events.event_input(Quartz.kCGEventKeyDown, own))

    def test_names_and_printable_keys(self):
        self.assertEqual(mac_events.describe_key("key:54"), "Right Command")
        self.assertEqual(mac_events.describe_key("mouse:3"), "Mouse back button")
        self.assertEqual(mac_events.describe_key("mouse:7"), "Mouse button 8")
        self.assertEqual(mac_events.describe_key("key:5"), "G")
        self.assertEqual(mac_events.describe_key("key:42"), "\\")
        self.assertTrue(mac_events.is_printable_key("key:0"))  # A
        self.assertTrue(mac_events.is_printable_key("key:49"))  # Space
        self.assertFalse(mac_events.is_printable_key("key:100"))  # F8
        self.assertFalse(mac_events.is_printable_key("mouse:3"))


@unittest.skipUnless(settings.IS_MAC, "macOS shortcut hiding")
class ShortcutHidingTest(unittest.TestCase):
    def listener(self, shortcut):
        from claude_dictation_anywhere import hotkeys

        with patch.object(mac_events, "EventTap") as tap:
            listener = hotkeys.TriggerListener({"shortcut_key": shortcut}, Mock(), Mock(), Mock())
        return listener, tap.call_args.kwargs["can_hide"]

    def test_letter_is_hidden_only_while_the_modifiers_are_held(self):
        listener, can_hide = self.listener("key:55+key:58+key:5")  # Command + Option + G
        self.assertTrue(can_hide)
        self.assertFalse(listener._handle("key:5", True, False))  # G alone types G
        self.assertFalse(listener._handle("key:5", False, False))
        self.assertFalse(listener._handle("key:55", True, False))  # modifiers pass through
        self.assertFalse(listener._handle("key:58", True, False))
        self.assertTrue(listener._handle("key:5", True, False))
        self.assertTrue(listener._handle("key:5", True, True))  # its auto-repeat too
        self.assertTrue(listener._handle("key:5", False, False))
        listener.combination.on_down.assert_called_once()
        listener.combination.on_up.assert_called_once()

    def test_modifier_only_shortcut_never_hides(self):
        _listener, can_hide = self.listener("key:59+key:58")
        self.assertFalse(can_hide)


@unittest.skipUnless(settings.IS_MAC, "macOS permission help")
class PermissionHelpTest(unittest.TestCase):
    def test_names_the_first_app_bundle_among_parents(self):
        chain = {
            "500": "400 /bin/zsh",
            "400": "1 /Applications/Ghostty.app/Contents/MacOS/ghostty",
        }

        def ps(args, **_kwargs):
            return Mock(stdout=" " + chain[args[-1]] + "\n")

        mac_events.launching_app_name.cache_clear()
        try:
            with (
                patch.object(mac_events.os, "getppid", return_value=500),
                patch.object(mac_events.subprocess, "run", side_effect=ps),
            ):
                self.assertEqual(mac_events.launching_app_name(), "Ghostty")
                self.assertIn("turn on Ghostty", mac_events.permission_help("Accessibility"))
        finally:
            mac_events.launching_app_name.cache_clear()

    def test_setup_warnings_carry_the_permission(self):
        with patch.object(mac_events, "missing_permissions", return_value=["Accessibility"]):
            warnings = output.setup_warnings(dict(settings.DEFAULT_SETTINGS))
        self.assertEqual([w["permission"] for w in warnings], ["Accessibility"])


@unittest.skipUnless(settings.IS_MAC, "macOS listener")
class ListenerLossTest(unittest.TestCase):
    def listener(self, shortcut="key:100"):
        from claude_dictation_anywhere import hotkeys

        with patch.object(mac_events, "EventTap") as tap:
            tap.return_value.can_hide = True
            tap.return_value.hides = False
            return hotkeys.TriggerListener({"shortcut_key": shortcut}, Mock(), Mock(), Mock())

    def test_stop_during_a_hold_reports_lost_once_and_clears_the_keys(self):
        listener = self.listener()
        listener._handle("key:100", True, False)
        self.assertEqual(listener.held_keys(), {"key:100"})
        listener.stop()
        listener.stop()
        listener.on_lost.assert_called_once()
        listener.combination.on_up.assert_not_called()
        self.assertEqual(listener.held_keys(), frozenset())

    def test_tap_switched_off_loses_track_so_the_next_real_press_works(self):
        listener = self.listener()
        listener._handle("key:100", True, False)
        listener.combination.on_down.assert_called_once()
        listener._lost_track()  # the release went missing while the tap was off
        listener.on_lost.assert_called_once()
        self.assertEqual(listener.held_keys(), frozenset())
        listener._handle("key:100", True, False)
        self.assertEqual(listener.combination.on_down.call_count, 2)

    def test_warns_when_the_key_cannot_be_hidden(self):
        listener = self.listener()
        self.assertIn("Accessibility", listener.warning)
        listener._tap.hides = True
        self.assertEqual(listener.warning, "")


@unittest.skipUnless(settings.IS_MAC, "macOS event tap")
class EventTapLifecycleTest(unittest.TestCase):
    """The tap with Quartz's slow or failing calls replaced; no permission needed."""

    def setUp(self):
        self.created = threading.Event()
        self.added = Mock()
        self.invalidated = Mock()
        patches = [
            patch.object(mac_events.Quartz, "CGPreflightListenEventAccess", return_value=True),
            patch.object(mac_events.Quartz, "CFMachPortCreateRunLoopSource", return_value="source"),
            patch.object(mac_events.Quartz, "CFRunLoopAddSource", self.added),
            patch.object(mac_events.Quartz, "CFRunLoopRemoveSource"),
            patch.object(mac_events.Quartz, "CGEventTapEnable"),
            patch.object(mac_events.Quartz, "CFMachPortInvalidate", self.invalidated),
            patch.object(mac_events, "can_type", return_value=True),
            patch.object(mac_events, "TAP_RUN_SLICE_S", 0.01),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tap_create(self, delay=0.0, result="tap"):
        def create(*_args):
            time.sleep(delay)
            self.created.set()
            return result

        return patch.object(mac_events.Quartz, "CGEventTapCreate", create)

    def test_start_that_times_out_never_becomes_active_later(self):
        with (
            self.tap_create(delay=0.2),
            patch.object(mac_events, "TAP_START_TIMEOUT_S", 0.01),
        ):
            tap = mac_events.EventTap(Mock())
            with self.assertRaises(mac_events.TapStartTimeout):
                tap.start()
            self.assertTrue(self.created.wait(2))
            tap._thread.join(2)
        self.assertFalse(tap.is_alive())
        self.added.assert_not_called()
        self.invalidated.assert_called_once_with("tap")

    def test_timeout_is_not_reported_as_a_missing_permission(self):
        with (
            self.tap_create(delay=0.2),
            patch.object(mac_events, "TAP_START_TIMEOUT_S", 0.01),
            self.assertRaises(mac_events.TapStartTimeout) as caught,
        ):
            mac_events.EventTap(Mock()).start()
        self.assertNotIsInstance(caught.exception, PermissionError)

    def test_refused_tap_reports_the_permission_and_leaves_no_thread(self):
        with self.tap_create(result=None):
            tap = mac_events.EventTap(Mock())
            with self.assertRaises(mac_events.PermissionMissing):
                tap.start()
        self.assertFalse(tap.is_alive())

    def test_normal_start_and_stop_release_everything_and_can_repeat(self):
        with self.tap_create():
            tap = mac_events.EventTap(Mock(), can_hide=True)
            tap.start()
            self.assertTrue(tap.is_alive())
            self.assertTrue(tap.hides)
            tap.stop()
            tap.stop()
        self.assertFalse(tap.is_alive())
        self.invalidated.assert_called_once_with("tap")

    def test_stop_right_after_start_cannot_be_missed(self):
        # CFRunLoopStop would do nothing to a loop that has not started running yet.
        for _ in range(20):
            with self.tap_create():
                tap = mac_events.EventTap(Mock())
                tap.start()
                tap.stop()
                self.assertFalse(tap.is_alive())

    def test_without_accessibility_it_only_listens_and_says_so(self):
        with self.tap_create(), patch.object(mac_events, "can_type", return_value=False):
            tap = mac_events.EventTap(Mock(), can_hide=True)
            tap.start()
            self.addCleanup(tap.stop)
        self.assertFalse(tap.hides)

    def test_permission_prompts_are_asked_only_once(self):
        mac_events._asked_permissions.clear()
        self.addCleanup(mac_events._asked_permissions.clear)
        request = Mock()
        with (
            patch.object(mac_events.Quartz, "CGPreflightListenEventAccess", return_value=False),
            patch.object(mac_events.Quartz, "CGRequestListenEventAccess", request),
        ):
            for _ in range(5):
                with self.assertRaises(mac_events.PermissionMissing):
                    mac_events.EventTap(Mock()).start()
        request.assert_called_once()

    def test_switched_off_tap_is_turned_back_on_and_reports_the_gap(self):
        interrupted = Mock()
        with self.tap_create():
            tap = mac_events.EventTap(Mock(), on_interrupted=interrupted)
            tap.start()
            self.addCleanup(tap.stop)
            mac_events.Quartz.CGEventTapEnable.reset_mock()
            event_type = mac_events.Quartz.kCGEventTapDisabledByTimeout
            self.assertEqual(tap._callback(None, event_type, "event", None), "event")
        mac_events.Quartz.CGEventTapEnable.assert_called_once_with("tap", True)
        interrupted.assert_called_once()

    def test_failing_handler_never_swallows_the_users_typing(self):
        tap = mac_events.EventTap(Mock(side_effect=KeyError("bug")))
        with (
            patch.object(mac_events, "event_input", return_value=("key:1", True, False)),
            self.assertLogs("claude_dictation_anywhere.mac_events", "ERROR"),
        ):
            self.assertEqual(tap._callback(None, 1, "event", None), "event")


@unittest.skipUnless(settings.IS_MAC, "macOS typing")
class TypeTextTest(unittest.TestCase):
    def test_interruption_stops_typing_and_reports_how_far_it_got(self):
        posted = []
        answers = iter([False, False, True])
        with (
            patch.object(mac_events, "_post_key", lambda _c, _f, text="": posted.append(text)),
            patch.object(mac_events, "TYPE_KEY_DELAY_S", 0),
        ):
            typed = mac_events.type_text("abcdefgh", lambda: next(answers), 2)
        self.assertEqual(typed, 4)
        self.assertEqual("".join(posted), "abcd")

    def test_switching_apps_while_typing_is_an_uncertain_failure(self):
        fronts = iter([111, 111, 222])
        with (
            patch.object(mac_events, "can_type", return_value=True),
            patch.object(mac_events, "_post_key"),
            patch.object(mac_events, "TYPE_KEY_DELAY_S", 0),
            patch.object(output, "focused_app", lambda: next(fronts)),
            patch.object(output, "TYPE_FOCUS_CHECK_CHARS", 1),
            self.assertRaises(output.InsertionFailed) as caught,
        ):
            output.insert_text("abcdef", "type", target_app=111)
        self.assertTrue(caught.exception.uncertain)

    def test_missing_accessibility_is_not_uncertain_and_asks_once(self):
        with (
            patch.object(mac_events, "can_type", return_value=False),
            patch.object(mac_events, "request_typing_permission") as request,
            self.assertRaises(output.InsertionFailed) as caught,
        ):
            output.insert_text("abc", "type")
        self.assertFalse(caught.exception.uncertain)
        request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
