"""Unit tests for the parts that need no microphone, input device, tray, or Claude Code."""

import json
import os
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from claude_dictation_anywhere import claude_session, history, hotkeys, output, settings


class ScreenParsingTest(unittest.TestCase):
    def test_strip_ansi_leaves_visible_text(self):
        raw = "\x1b[2J\x1b[1;1H\x1b[38;5;174m❯\x1b[0m hello\x1b]0;title\x07 world\x1b7\x1b8"
        self.assertEqual(claude_session.strip_ansi(raw), "❯ hello world")

    def test_screen_shows_ignores_missing_spaces(self):
        self.assertTrue(
            claude_session.screen_shows(
                "❯No,exit Yes,Itrustthisfolder", claude_session.TRUST_MARKER
            )
        )
        self.assertFalse(claude_session.screen_shows("❯ Try it", claude_session.LOGGED_OUT_MARKER))


class ClaudeCommandTest(unittest.TestCase):
    def test_env_drops_parent_session_variables(self):
        env = claude_session.build_claude_env(
            {
                "PATH": "/usr/bin",
                "CLAUDE_CODE_OAUTH_TOKEN": "x",
                "ANTHROPIC_API_KEY": "y",
            },
            Path("/hook.sh"),
            Path("/result.txt"),
        )
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertFalse([k for k in env if k.startswith(("CLAUDE", "ANTHROPIC"))])
        self.assertEqual(env["EDITOR"], "/hook.sh")
        self.assertEqual(env["CDA_RESULT"], "/result.txt")

    def test_args_hold_mode_no_auto_submit_no_tools(self):
        args = claude_session.build_claude_args("/usr/bin/claude", "fr")
        voice = json.loads(args[args.index("--settings") + 1])
        self.assertEqual(voice["voice"], {"enabled": True, "mode": "hold", "autoSubmit": False})
        self.assertEqual(voice["language"], "fr")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[0], "/usr/bin/claude")

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
    def test_make_typeable(self):
        self.assertEqual(output.make_typeable("“It’s—fine…”"), '"It\'s-fine..."')

    def test_describe_shortcut(self):
        self.assertEqual(
            hotkeys.describe_shortcut("KEY_F8", "Keychron  Link  Keyboard"),
            "F8 on Keychron Link Keyboard",
        )
        self.assertEqual(hotkeys.describe_shortcut("BTN_SIDE", ""), "Mouse back button")

    @unittest.skipIf(settings.IS_MAC, "Linux key codes")
    def test_printable_keys_are_refused_as_shortcuts(self):
        self.assertTrue(hotkeys.is_printable_key("KEY_A"))
        self.assertTrue(hotkeys.is_printable_key("KEY_SPACE"))
        self.assertFalse(hotkeys.is_printable_key("KEY_F8"))
        self.assertFalse(hotkeys.is_printable_key("BTN_SIDE"))


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


class MuteWhileDictatingTest(unittest.TestCase):
    def run_with_output(self, muted_before: bool):
        """Mute then restore; returns the set_output_muted calls."""
        from claude_dictation_anywhere import system_audio

        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}),
            unittest.mock.patch.object(system_audio, "output_muted", return_value=muted_before),
            unittest.mock.patch.object(system_audio, "set_output_muted") as set_muted,
        ):
            system_audio.mute_output()
            system_audio.restore_output()
            system_audio.restore_output()  # a second restore (e.g. at shutdown) does nothing
            return [call.args[0] for call in set_muted.call_args_list]

    def test_playing_output_is_muted_then_unmuted_once(self):
        self.assertEqual(self.run_with_output(muted_before=False), [True, False])

    def test_already_muted_output_is_left_alone(self):
        self.assertEqual(self.run_with_output(muted_before=True), [])

    def test_failing_mixer_never_raises(self):
        from claude_dictation_anywhere import system_audio

        with unittest.mock.patch.object(system_audio, "output_muted", side_effect=OSError("x")):
            system_audio.mute_output()


class RecordingStartTest(unittest.TestCase):
    def test_spaces_start_before_slow_feedback_and_quick_press_unmutes(self):
        from claude_dictation_anywhere import controller as controller_module

        calls = []  # (what, seconds since the press)

        class FakeSession:
            def is_alive(self):
                return True

            def begin_hold(self):
                pass

            def press_space(self):
                calls.append(("space", time.monotonic() - pressed))

            def collect_transcript(self):
                return ""

        def slow_sound(*_args, **_kwargs):
            time.sleep(0.3)

        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}),
            unittest.mock.patch.object(controller_module, "find_claude", return_value=""),
            unittest.mock.patch.object(controller_module.feedback, "play_sound", slow_sound),
            unittest.mock.patch.object(
                controller_module.system_audio,
                "mute_output",
                lambda: calls.append(("mute", time.monotonic() - pressed)),
            ),
            unittest.mock.patch.object(
                controller_module.system_audio,
                "restore_output",
                lambda: calls.append(("restore", time.monotonic() - pressed)),
            ),
        ):
            controller = controller_module.DictationController(dict(settings.DEFAULT_SETTINGS))
            controller.settings["notifications"] = False
            controller.session = FakeSession()
            controller.events.put("up")  # released almost at once
            pressed = time.monotonic()
            controller._dictate_once()

        names = [name for name, _ in calls]
        self.assertEqual(names[0], "space")
        self.assertLess(calls[0][1], 0.05)
        self.assertLess(names.index("mute"), names.index("restore"))
