"""The session driving a scripted fake Claude Code in a real pty: no network, no microphone."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

from claude_dictation_anywhere import claude_session
from claude_dictation_anywhere.claude_session import (
    ClaudeVoiceSession,
    DictationError,
    PartialTranscript,
)

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
HOLD_SECONDS = 0.25


class FakeClaudeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.keylog = root / "keys"
        self.keylog.touch()
        self.program = root / "claude"
        self.program.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_CLAUDE}" "$@"\n')
        self.program.chmod(0o700)
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(root),
                "XDG_RUNTIME_DIR": str(root / "run"),
                "XDG_DATA_HOME": str(root / "data"),
                "FAKE_CLAUDE_KEYLOG": str(self.keylog),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        (root / "run").mkdir()
        self.sessions = []

    def session(self, **options) -> ClaudeVoiceSession:
        os.environ["FAKE_CLAUDE_OPTIONS"] = json.dumps(options)
        session = ClaudeVoiceSession(str(self.program), "")
        self.sessions.append(session)
        self.addCleanup(session.stop)
        return session

    def started(self, **options) -> ClaudeVoiceSession:
        session = self.session(**options)
        session.start()
        return session

    def hold(self, session, seconds=HOLD_SECONDS):
        session.begin_hold()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            session.press_space()
            time.sleep(claude_session.KEY_REPEAT_INTERVAL_S)

    def dictate(self, session, seconds=HOLD_SECONDS) -> str:
        self.hold(session, seconds)
        return session.collect_transcript()

    def keys(self) -> bytes:
        return self.keylog.read_bytes()


class NormalDictationTest(FakeClaudeCase):
    def test_transcript_arrives_and_no_prompt_is_ever_submitted(self):
        session = self.started(transcript="hello world")
        self.assertEqual(self.dictate(session), "hello world")
        self.assertNotIn(b"\r", self.keys())
        self.assertNotIn(b"\n", self.keys())

    def test_next_recording_holds_only_its_own_text(self):
        session = self.started(transcript="same words")
        self.assertEqual(self.dictate(session), "same words")
        self.assertEqual(self.dictate(session), "same words")  # not "same wordssame words"

    def test_long_wrapped_transcript_comes_through_whole(self):
        text = "word " * 60
        session = self.started(transcript=text)
        self.assertEqual(self.dictate(session), text.strip())
        self.assertTrue(session.running)

    def test_final_text_after_a_slow_silent_network_is_not_cut_short(self):
        # 1.2 s of silent processing: the old 0.6 s quiet rule pulled the text too early.
        session = self.started(transcript="final words", interim="partial", processing_s=1.2)
        self.assertEqual(self.dictate(session), "final words")

    def test_transcript_with_status_words_is_not_taken_for_a_state(self):
        text = "I am listening… No speech detected"
        session = self.started(transcript=text)
        self.assertEqual(self.dictate(session), text)

    def test_nothing_heard_is_empty_not_an_error(self):
        session = self.started(transcript="")
        self.assertEqual(self.dictate(session), "")

    def test_version_without_the_processing_state_still_works(self):
        session = self.started(transcript="hello", no_processing=True)
        self.assertEqual(self.dictate(session), "hello")

    def test_very_short_press_returns_quickly(self):
        session = self.started(transcript="never recorded")
        session.begin_hold()
        session.press_space()
        self.assertEqual(session.collect_transcript(), "")

    def test_hand_off_folder_is_private_and_removed_with_the_session(self):
        session = self.started(transcript="secret words")
        folder = session.handoff_dir
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(self.dictate(session), "secret words")
        self.assertEqual(list(folder.glob("result*")), [])
        session.stop()
        self.assertFalse(folder.exists())

    def test_hand_off_folder_removed_by_a_cleaner_comes_back(self):
        session = self.started(transcript="still here")
        self.hold(session)
        subprocess.run(["rm", "-rf", str(session.handoff_dir)], check=True)
        self.assertEqual(session.collect_transcript(), "still here")


class FailureTest(FakeClaudeCase):
    def test_partial_text_with_an_error_notice_is_reported_as_partial(self):
        session = self.started(transcript="half a sent", notice="Voice connection failed")
        with self.assertRaises(PartialTranscript) as caught:
            self.dictate(session)
        self.assertEqual(caught.exception.text, "half a sent")
        self.assertNotIn("half a sent", str(caught.exception))

    def test_error_notice_without_text_is_an_error(self):
        session = self.started(transcript="", notice="No speech detected")
        with self.assertRaisesRegex(DictationError, "No speech detected"):
            self.dictate(session)

    def test_earlier_notice_still_on_screen_does_not_fail_the_next_recording(self):
        session = self.started(sticky_notice="No speech detected")
        session.begin_hold()  # the notice is already up, so it is not this recording's error
        self.assertEqual(session.collect_transcript(), "")

    def test_transcript_that_never_finishes_is_partial_and_replaces_the_session(self):
        with patch.object(claude_session, "FINALIZE_TIMEOUT_S", 0.6):
            session = self.started(redraw_forever=True, transcript="late")
            self.hold(session)
            with self.assertRaises((PartialTranscript, DictationError)):
                session.collect_transcript()
        self.assertFalse(session.running)

    def test_ctrl_g_ignored_saves_the_visible_text_before_stopping_the_session(self):
        with patch.object(claude_session, "EDITOR_TIMEOUT_S", 0.3):
            session = self.started(transcript="only copy of my words", ignore_ctrl_g=True)
            with self.assertRaisesRegex(PartialTranscript, "did not hand over") as caught:
                self.dictate(session)
        self.assertEqual(caught.exception.text, "only copy of my words")
        self.assertFalse(session.running)

    def test_ctrl_g_ignored_with_an_empty_box_is_a_plain_error(self):
        with patch.object(claude_session, "EDITOR_TIMEOUT_S", 0.3):
            session = self.started(transcript="", ignore_ctrl_g=True)
            with self.assertRaises(DictationError) as caught:
                self.dictate(session)
        self.assertNotIsInstance(caught.exception, PartialTranscript)
        self.assertFalse(session.running)

    def test_stuck_session_is_replaced_when_a_control_key_cannot_be_sent(self):
        with patch.object(claude_session, "WRITE_TIMEOUT_S", 0.05):
            session = self.started(stop_reading=True)
            session.begin_hold()
            for _ in range(20_000):
                session.press_space()  # fills the pty
            with self.assertRaisesRegex(DictationError, "stopped accepting keys"):
                session._write_key(claude_session.CTRL_G)
        self.assertFalse(session.running)

    def test_stuck_session_during_collect_saves_the_text_and_is_replaced(self):
        with patch.object(claude_session, "WRITE_TIMEOUT_S", 0.05):
            session = self.started(transcript="words", ignore_ctrl_g=True)
            self.hold(session)
            with (
                patch.object(session, "_write", return_value=False),
                self.assertRaises(PartialTranscript) as caught,
            ):
                session.collect_transcript()
        self.assertEqual(caught.exception.text, "words")
        self.assertFalse(session.running)

    def test_unfinished_transcript_replaces_the_session_even_if_the_hand_off_fails(self):
        with (
            patch.object(claude_session, "FINALIZE_TIMEOUT_S", 0.3),
            patch.object(claude_session, "EDITOR_TIMEOUT_S", 0.2),
        ):
            session = self.started(redraw_forever=True, ignore_ctrl_g=True)
            self.hold(session)
            with self.assertRaises(DictationError):
                session.collect_transcript()
        self.assertFalse(session.running)

    def test_logged_out_notice_does_not_discard_text_that_exists(self):
        session = self.started(transcript="spoken words", notice="Not logged in")
        with self.assertRaises(PartialTranscript) as caught:
            self.dictate(session)
        self.assertEqual(caught.exception.text, "spoken words")
        self.assertIn("/login", str(caught.exception))

    def test_cancel_on_a_stuck_session_stops_it_quietly(self):
        with patch.object(claude_session, "WRITE_TIMEOUT_S", 0.05):
            session = self.started(stop_reading=True)
            session.begin_hold()
            for _ in range(20_000):
                session.press_space()
            session.cancel_recording()
        self.assertFalse(session.running)

    def test_freeze_at_microphone_is_reported_and_replaced(self):
        with patch.object(claude_session, "WARMUP_FREEZE_TIMEOUT_S", 0.2):
            session = self.started(freeze_warmup=True)
            with self.assertRaisesRegex(DictationError, "microphone permission"):
                self.dictate(session)
        self.assertFalse(session.running)

    def test_full_pty_cannot_block_the_recording_loop(self):
        session = self.started(stop_reading=True)
        session.begin_hold()
        started = time.monotonic()
        for _ in range(20_000):  # far more than any pty buffer holds
            session.press_space()
        self.assertLess(time.monotonic() - started, 5)

    def test_child_dying_during_a_hold_raises_a_dictation_error(self):
        session = self.started()
        session.begin_hold()
        os.kill(session.pid, 9)
        with self.assertRaises(DictationError):
            for _ in range(200):
                session.press_space()
                time.sleep(0.01)

    def test_cancel_discards_the_recording_with_escape(self):
        session = self.started(transcript="must not appear")
        self.hold(session)  # still within RELEASE_GAP_S of the last Space
        session.cancel_recording()
        self.assertIn(b"\x1b", self.keys())
        self.assertEqual(session.collect_transcript(), "")


class StartupTest(FakeClaudeCase):
    def test_trust_dialog_is_answered_only_after_reading_the_arrow_on_yes(self):
        session = self.started(startup="trust", transcript="ok")
        self.assertEqual(self.keys(), b"\x1b[B\r")
        self.assertEqual(self.dictate(session), "ok")

    def test_unknown_menu_gets_no_key_at_all(self):
        with patch.object(claude_session, "STARTUP_TIMEOUT_S", 0.4):
            session = self.session(startup="menu")
            with self.assertRaisesRegex(DictationError, "does not recognise"):
                session.start()
        self.assertEqual(self.keys(), b"")
        self.assertFalse(session.running)

    def test_changed_trust_dialog_gets_no_key_at_all(self):
        with patch.object(claude_session, "STARTUP_TIMEOUT_S", 0.4):
            session = self.session(startup="trust_changed")
            with self.assertRaises(DictationError):
                session.start()
        self.assertEqual(self.keys(), b"")

    def test_logged_out_notice_is_explained(self):
        with self.assertRaisesRegex(DictationError, "not logged in"):
            self.session(startup="logged_out").start()

    def test_exit_at_startup_and_old_version_are_explained(self):
        with self.assertRaisesRegex(DictationError, "exited at startup"):
            self.session(startup="exit").start()
        with self.assertRaisesRegex(DictationError, "claude update"):
            self.session(startup="unknown_option").start()

    def test_program_that_cannot_run_never_returns_into_this_program(self):
        broken = Path(self.tmp.name) / "broken"
        broken.write_text("not a program")
        broken.chmod(0o700)
        session = ClaudeVoiceSession(str(broken), "")
        self.addCleanup(session.stop)
        with self.assertRaises(DictationError):
            session.start()

    def test_error_messages_hold_no_terminal_text(self):
        with patch.object(claude_session, "STARTUP_TIMEOUT_S", 0.4):
            session = self.session(startup="menu")
            with self.assertRaises(DictationError) as caught:
                session.start()
        self.assertNotIn("Dark mode", str(caught.exception))

    def test_window_size_is_set_before_the_program_starts(self):
        script = Path(self.tmp.name) / "size.py"
        script.write_text(
            "import os,sys,struct,fcntl,termios\n"
            "size=struct.unpack('HHHH',fcntl.ioctl(0,termios.TIOCGWINSZ,b'\\0'*8))\n"
            f"open({str(self.keylog)!r},'w').write(str(size[:2]))\n"
        )
        self.program.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{script}"\n')
        session = self.session()
        with self.assertRaises(DictationError):
            session.start()
        self.assertEqual(self.keylog.read_text(), "(24, 100)")


class StopTest(FakeClaudeCase):
    def test_stop_is_repeatable_and_the_session_refuses_keys_afterwards(self):
        session = self.started()
        session.stop()
        session.stop()
        self.assertFalse(session.is_alive())
        self.assertFalse(session.running)
        self.assertIsNone(session.fd)
        with self.assertRaises(DictationError):
            session.press_space()

    def test_program_that_ignores_sigterm_is_killed(self):
        session = self.started(ignore_term=True)
        pid = session.pid
        session.stop()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_helpers_in_its_process_group_stop_too(self):
        session = self.started(spawn_helper=True)
        helper = int(Path(str(self.keylog) + ".helper").read_text())
        session.stop()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(helper, 0)
            except OSError:
                return
            time.sleep(0.02)
        self.fail("the helper process survived")

    def test_stop_during_startup_ends_it_promptly(self):
        session = self.session(startup="menu")
        errors = []

        def run():
            try:
                session.start()
            except DictationError as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.2)
        began = time.monotonic()
        session.stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - began, 3)
        self.assertEqual(len(errors), 1)

    def test_stop_during_collect_ends_it_promptly(self):
        session = self.started(processing_s=30)
        self.hold(session)
        errors = []

        def run():
            try:
                session.collect_transcript()
            except DictationError as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.1)
        session.stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)

    def test_status_polls_during_stop_do_not_race(self):
        session = self.started()
        done = threading.Event()
        problems = []

        def poll():
            while not done.is_set():
                try:
                    session.is_alive()
                    _ = session.running
                except Exception as error:  # noqa: BLE001 - the point of this test
                    problems.append(error)

        pollers = [threading.Thread(target=poll) for _ in range(4)]
        for thread in pollers:
            thread.start()
        session.stop()
        done.set()
        for thread in pollers:
            thread.join()
        self.assertEqual(problems, [])

    def test_stale_hand_offs_of_an_earlier_run_are_removed(self):
        stale = claude_session.runtime_dir() / f"{claude_session.HANDOFF_DIR_PREFIX}old"
        stale.mkdir()
        (stale / "result.1").write_text("old transcript")
        claude_session.remove_stale_handoffs()
        self.assertFalse(stale.exists())


class EditorHookTest(unittest.TestCase):
    """The hook script itself, run the way Claude Code runs it, with each step made to fail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.handoff = self.root / "handoff"
        self.handoff.mkdir()
        (self.handoff / "generation").write_text("7")
        self.hook = self.root / "hook.sh"
        self.hook.write_text(claude_session.EDITOR_HOOK_SCRIPT)
        self.hook.chmod(0o700)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("my words")
        self.addCleanup(self.handoff.chmod, 0o700)
        self.addCleanup(self.prompt.chmod, 0o600)

    def run_hook(self, path=None):
        env = {"CDA_HANDOFF_DIR": str(self.handoff), "PATH": path or os.environ["PATH"]}
        return subprocess.run([str(self.hook), "--flag", str(self.prompt)], env=env, check=False)

    def test_success_publishes_then_clears_then_acknowledges(self):
        self.assertEqual(self.run_hook().returncode, 0)
        self.assertEqual((self.handoff / "result.7").read_text(), "my words")
        self.assertEqual(self.prompt.read_text(), "")
        self.assertTrue((self.handoff / "done.7").exists())
        self.assertEqual(stat.S_IMODE((self.handoff / "result.7").stat().st_mode) & 0o077, 0)

    def test_missing_folder_fails_and_keeps_the_prompt(self):
        self.handoff.rename(self.root / "gone")
        self.handoff = self.root / "handoff"
        self.addCleanup(self.handoff.mkdir)
        self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertEqual(self.prompt.read_text(), "my words")

    def test_failed_copy_keeps_the_prompt(self):
        self.handoff.chmod(0o500)
        self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertEqual(self.prompt.read_text(), "my words")
        self.assertFalse((self.handoff / "done.7").exists())

    def test_failed_rename_keeps_the_prompt_and_the_copy(self):
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "mv").write_text("#!/bin/sh\nexit 1\n")
        (tools / "mv").chmod(0o700)
        (tools / "cp").symlink_to(subprocess.check_output(["which", "cp"], text=True).strip())
        (tools / "cat").symlink_to(subprocess.check_output(["which", "cat"], text=True).strip())
        self.assertNotEqual(self.run_hook(str(tools)).returncode, 0)
        self.assertEqual(self.prompt.read_text(), "my words")
        self.assertEqual((self.handoff / "result.7.partial").read_text(), "my words")
        self.assertFalse((self.handoff / "result.7").exists())

    def test_failed_clear_leaves_the_published_copy_without_acknowledging(self):
        self.prompt.chmod(0o400)
        self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertEqual((self.handoff / "result.7").read_text(), "my words")
        self.assertEqual(self.prompt.read_text(), "my words")
        self.assertFalse((self.handoff / "done.7").exists())


class HandoffRecoveryTest(unittest.TestCase):
    """What the session does with each state a failed hook can leave behind."""

    def session(self, root: Path) -> ClaudeVoiceSession:
        session = object.__new__(ClaudeVoiceSession)
        session.rows = ()
        session.stop = unittest.mock.Mock()
        session.handoff_dir = root
        return session

    def test_published_copy_without_acknowledgement_is_returned_and_session_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "result.1").write_text("all my words")
            session = self.session(root)
            text = session._recover_handoff(root / "result.1", root / "result.1.partial", "")
        self.assertEqual(text, "all my words")
        session.stop.assert_called_once()

    def test_partial_copy_is_offered_as_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "result.1.partial").write_text("half")
            session = self.session(root)
            with self.assertRaises(PartialTranscript) as caught:
                session._recover_handoff(root / "result.1", root / "result.1.partial", "seen")
        self.assertEqual(caught.exception.text, "half")
        session.stop.assert_called_once()

    def test_empty_partial_copy_falls_back_to_what_the_screen_showed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "result.1.partial").write_text("")
            session = self.session(root)
            with self.assertRaises(PartialTranscript) as caught:
                session._recover_handoff(root / "result.1", root / "result.1.partial", "seen")
        self.assertEqual(caught.exception.text, "seen")

    def test_nothing_anywhere_is_a_plain_error_not_an_empty_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "result.1.partial").write_text("  ")
            session = self.session(root)
            with self.assertRaises(DictationError) as caught:
                session._recover_handoff(root / "result.1", root / "result.1.partial", "")
        self.assertNotIsInstance(caught.exception, PartialTranscript)

    def test_invalid_utf8_is_replaced_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result"
            path.write_bytes(b"caf\xe9")
            self.assertEqual(ClaudeVoiceSession._read_handoff(path), "caf\ufffd")

    def test_unreadable_result_is_a_dictation_error(self):
        with self.assertRaises(DictationError):
            ClaudeVoiceSession._read_handoff(Path("/nonexistent/result"))


class FindClaudeTest(unittest.TestCase):
    def test_unusable_paths_are_not_found_rather_than_a_crash(self):
        for path in ("~no-such-user-here/claude", "/tmp/a\0b", "/nonexistent/claude"):
            with self.subTest(path):
                self.assertIsNone(claude_session.find_claude(path))


class EnvironmentTest(unittest.TestCase):
    def test_parent_session_variables_go_but_chosen_config_stays(self):
        env = claude_session.build_claude_env(
            {
                "PATH": "/usr/bin",
                "CLAUDE_CODE_OAUTH_TOKEN": "x",
                "ANTHROPIC_API_KEY": "y",
                "ANTHROPIC_BASE_URL": "z",
                "CLAUDE_CONFIG_DIR": "/home/me/.claude-work",
            },
            Path("/hook.sh"),
            Path("/handoff"),
        )
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertFalse(
            {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"} & env.keys()
        )
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/me/.claude-work")
        self.assertEqual(env["EDITOR"], "/hook.sh")
        self.assertEqual(env["CDA_HANDOFF_DIR"], "/handoff")

    def test_args_hold_mode_no_auto_submit_no_tools(self):
        args = claude_session.build_claude_args("/usr/bin/claude", "fr")
        voice = json.loads(args[args.index("--settings") + 1])
        self.assertEqual(voice["voice"], {"enabled": True, "mode": "hold", "autoSubmit": False})
        self.assertEqual(voice["language"], "fr")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--safe-mode", args)


if __name__ == "__main__":
    unittest.main()
