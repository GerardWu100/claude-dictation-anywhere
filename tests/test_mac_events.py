"""Check macOS shortcut parsing, naming, and the app-switch guard with synthetic Quartz events."""

import unittest
from unittest.mock import patch

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
        self.assertEqual(mac_events.event_input(changed, down), ("key:54", True))
        up = modifier_event(54, COMMAND | LEFT_COMMAND_BIT)
        self.assertEqual(mac_events.event_input(changed, up), ("key:54", False))

    def test_side_mouse_button_keeps_its_number(self):
        for event_type, pressed in (
            (Quartz.kCGEventOtherMouseDown, True),
            (Quartz.kCGEventOtherMouseUp, False),
        ):
            event = _as_hardware(Quartz.CGEventCreateMouseEvent(None, event_type, (0, 0), 3))
            self.assertEqual(mac_events.event_input(event_type, event), ("mouse:3", pressed))

    def test_auto_repeat_and_own_events_are_dropped(self):
        repeat = _as_hardware(Quartz.CGEventCreateKeyboardEvent(None, 100, True))
        Quartz.CGEventSetIntegerValueField(repeat, Quartz.kCGKeyboardEventAutorepeat, 1)
        self.assertIsNone(mac_events.event_input(Quartz.kCGEventKeyDown, repeat))
        own = Quartz.CGEventCreateKeyboardEvent(None, 100, True)
        self.assertIsNone(mac_events.event_input(Quartz.kCGEventKeyDown, own))

    def test_names_and_printable_keys(self):
        self.assertEqual(mac_events.describe_key("key:54"), "Right Command")
        self.assertEqual(mac_events.describe_key("mouse:3"), "Mouse back button")
        self.assertEqual(mac_events.describe_key("mouse:7"), "Mouse button 8")
        self.assertTrue(mac_events.is_printable_key("key:0"))  # A
        self.assertTrue(mac_events.is_printable_key("key:49"))  # Space
        self.assertFalse(mac_events.is_printable_key("key:100"))  # F8
        self.assertFalse(mac_events.is_printable_key("mouse:3"))


@unittest.skipUnless(settings.IS_MAC, "macOS app-switch guard")
class AppSwitchGuardTest(unittest.TestCase):
    def setUp(self):
        self.settings = dict(settings.DEFAULT_SETTINGS)

    def test_switched_app_gets_clipboard_only(self):
        with (
            patch.object(output, "focused_app", return_value=222),
            patch.object(output, "copy_to_clipboard") as copy,
            patch.object(output, "insert_text") as insert,
            self.assertRaises(RuntimeError),
        ):
            output.deliver("hello", self.settings, target_app=111)
        copy.assert_called_once_with("hello")
        insert.assert_not_called()

    def test_same_app_is_typed(self):
        with (
            patch.object(output, "focused_app", return_value=111),
            patch.object(output, "copy_to_clipboard"),
            patch.object(output, "insert_text") as insert,
        ):
            output.deliver("hello", self.settings, target_app=111)
        insert.assert_called_once_with("hello", "type")


if __name__ == "__main__":
    unittest.main()
