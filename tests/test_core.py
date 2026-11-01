"""Unit tests for the parts that need no microphone, input device, tray, or Claude Code."""

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from claude_dictation_anywhere import claude_session, history, hotkeys, settings


class ClaudeCommandTest(unittest.TestCase):
    def test_find_claude_uses_configured_path_only_if_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / "claude"
            program.write_text("#!/bin/sh\n")
            self.assertIsNone(claude_session.find_claude(str(program)))
            program.chmod(0o755)
            self.assertEqual(claude_session.find_claude(str(program)), str(program))


class SettingsTest(unittest.TestCase):
    def test_check_rejects_bad_choice_and_type(self):
        with self.assertRaises(ValueError):
            settings.check_settings({"mode": "sometimes"})
        with self.assertRaises(ValueError):
            settings.check_settings({"sounds": "yes"})
        self.assertEqual(
            settings.check_settings({"mode": "toggle", "unknown": 1}),
            {"mode": "toggle"},
        )

    def test_save_then_load_round_trip_keeps_defaults_for_bad_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.toml"
            settings.save_settings(dict(settings.DEFAULT_SETTINGS, mode="toggle"), path)
            self.assertEqual(settings.load_settings(path)["mode"], "toggle")
            path.write_text('mode = "toggle"\nlanguage = "klingon"\n')
            loaded = settings.load_settings(path)
            self.assertEqual(loaded["mode"], "toggle")
            self.assertEqual(loaded["language"], settings.DEFAULT_SETTINGS["language"])


class HistoryTest(unittest.TestCase):
    def test_newest_first_trim_and_clear(self):
        with tempfile.TemporaryDirectory() as folder:
            log = history.History(Path(folder) / "history.jsonl", max_entries=3)
            for word in ("one", "two", "three", "four"):
                log.add(word, 1.0)
            self.assertEqual([e["text"] for e in log.recent(10)], ["four", "three", "two"])
            log.clear()
            self.assertEqual(log.recent(10), [])


class OutputAndShortcutTest(unittest.TestCase):
    @unittest.skipIf(settings.IS_MAC, "Linux key codes")
    def test_describe_shortcut(self):
        self.assertEqual(
            hotkeys.describe_shortcut("KEY_F8", "Keychron  Link  Keyboard"),
            "F8 on Keychron Link Keyboard",
        )
        self.assertEqual(hotkeys.describe_shortcut("BTN_SIDE", ""), "Mouse back button")

    @unittest.skipIf(settings.IS_MAC, "Linux key codes")
    def test_printable_keys_are_refused_as_shortcuts(self):
        self.assertTrue(hotkeys.is_printable_key("KEY_A"))
        self.assertTrue(hotkeys.shortcut_types_text("KEY_LEFTSHIFT+KEY_A"))
        self.assertFalse(hotkeys.shortcut_types_text("KEY_LEFTCTRL+KEY_A"))
        self.assertFalse(hotkeys.shortcut_types_text("KEY_LEFTSHIFT"))
        for key in ("KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_TAB", "KEY_ENTER", "KEY_BACKSPACE"):
            self.assertFalse(hotkeys.is_printable_key(key), key)
        self.assertTrue(hotkeys.is_printable_key("KEY_KP5"))
        self.assertTrue(hotkeys.is_printable_key("KEY_SPACE"))
        self.assertFalse(hotkeys.is_printable_key("KEY_F8"))
        self.assertFalse(hotkeys.is_printable_key("BTN_SIDE"))


class CombinationTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.combination = hotkeys.Combination(
            "a+b", lambda: self.events.append("down"), lambda: self.events.append("up")
        )

    def test_fires_once_all_keys_are_held_in_any_order(self):
        for key, pressed in (("b", True), ("x", True), ("a", True), ("a", True)):
            self.combination.feed(key, pressed)
        self.assertEqual(self.events, ["down"])

    def test_releases_when_any_key_is_let_go(self):
        for key, pressed in (("a", True), ("b", True), ("b", False), ("a", False)):
            self.combination.feed(key, pressed)
        self.assertEqual(self.events, ["down", "up"])

    def test_reset_forgets_held_keys_so_a_reconnect_cannot_look_like_a_press(self):
        self.combination.feed("a", True)
        self.combination.feed("b", True)
        self.combination.reset()
        self.assertEqual(self.combination.held, set())
        self.combination.feed("b", True, repeat=True)  # auto-repeat of a key still down
        self.combination.feed("a", False)
        self.assertEqual(self.events, ["down"])  # no second down, and no late up

    def test_one_key_alone_never_fires(self):
        self.combination.feed("a", True)
        self.combination.feed("a", False)
        self.assertEqual(self.events, [])

    def test_single_key_shortcut_still_works(self):
        events = []
        single = hotkeys.Combination(
            "a", lambda: events.append("down"), lambda: events.append("up")
        )
        single.feed("a", True)
        single.feed("a", False)
        self.assertEqual(events, ["down", "up"])

    def test_hideable_key_is_hidden_only_while_the_rest_is_held(self):
        combination = hotkeys.Combination(
            "ctrl+g", unittest.mock.Mock(), unittest.mock.Mock(), hideable={"g"}
        )
        self.assertFalse(combination.feed("g", True))  # G alone types G
        self.assertFalse(combination.feed("g", False))
        self.assertFalse(combination.feed("ctrl", True))  # modifiers always reach apps
        self.assertTrue(combination.feed("g", True))
        self.assertTrue(combination.feed("g", True, repeat=True))
        self.assertTrue(combination.feed("g", False))
        self.assertFalse(combination.feed("ctrl", False))
        combination.on_down.assert_called_once()
        combination.on_up.assert_called_once()


@unittest.skipUnless(settings.IS_MAC, "macOS key names")
class MacShortcutTest(unittest.TestCase):
    def test_combination_names_and_text_check(self):
        self.assertEqual(
            hotkeys.describe_shortcut("key:59+key:58", ""), "Left Control + Left Option"
        )
        self.assertFalse(hotkeys.shortcut_types_text("key:59+key:58"))
        self.assertFalse(hotkeys.shortcut_types_text("key:59+key:2"))  # Control + D
        self.assertTrue(hotkeys.shortcut_types_text("key:56+key:2"))  # Shift + D
        self.assertTrue(hotkeys.shortcut_types_text("key:49"))  # Space

    def test_shortcut_problems(self):
        self.assertIsNone(hotkeys.shortcut_problem("key:55+key:58+key:5"))
        self.assertIn("types text", hotkeys.shortcut_problem("key:49"))
        self.assertIn("Caps Lock", hotkeys.shortcut_problem("key:57"))
        self.assertIn("Caps Lock", hotkeys.shortcut_problem("key:55+key:57"))
        self.assertIn("Unknown", hotkeys.shortcut_problem("garbage"))
        self.assertIn("at least one", hotkeys.shortcut_problem(""))


if __name__ == "__main__":
    os.environ.setdefault("XDG_RUNTIME_DIR", tempfile.gettempdir())
    unittest.main()


class LauncherTest(unittest.TestCase):
    def test_install_writes_entry_and_icon_then_uninstall_removes_them(self):
        from claude_dictation_anywhere import launcher

        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}),
            unittest.mock.patch.object(launcher, "app_command", return_value="/x/cda"),
        ):
            launcher.install_launcher()
            entry = launcher.desktop_file_path().read_text()
            self.assertIn('Exec="/x/cda" --open\n', entry)
            self.assertIn(f"Icon={settings.APP_NAME}\n", entry)
            self.assertGreater(launcher.icon_path().stat().st_size, 0)
            launcher.uninstall_launcher()
            self.assertFalse(launcher.desktop_file_path().exists())
            self.assertFalse(launcher.icon_path().exists())
