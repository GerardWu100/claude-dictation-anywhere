"""The state machine with a fake session and listener: real worker thread, no Claude Code or keys."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch

from claude_dictation_anywhere import controller as controller_module
from claude_dictation_anywhere import output, settings
from claude_dictation_anywhere.claude_session import DictationError, PartialTranscript

WAIT_S = 5


def wait_until(condition, seconds=WAIT_S):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


class FakeSession:
    """Records what the controller does. Tests set the hooks to block or fail a step."""

    instances: ClassVar[list["FakeSession"]] = []
    transcript = "hello"
    start_gate: threading.Event | None = None
    collect_gate: threading.Event | None = None
    collect_error: Exception | None = None
    space_error: Exception | None = None

    def __init__(self, claude_path, language):
        self.claude_path, self.language = claude_path, language
        self.running = False
        self.spaces = 0
        self.collected = 0
        self.cancelled = 0
        self.stopped = threading.Event()
        FakeSession.instances.append(self)

    def start(self):
        while FakeSession.start_gate is not None and not FakeSession.start_gate.wait(0.01):
            if self.stopped.is_set():
                raise DictationError("stopped")
        if self.stopped.is_set():
            raise DictationError("stopped")
        self.running = True

    def is_alive(self):
        return self.running

    def ready_for_recording(self):
        return True

    def wait_ready_for_recording(self):
        return True

    def begin_hold(self):
        pass

    def press_space(self):
        if FakeSession.space_error is not None:
            error, FakeSession.space_error = FakeSession.space_error, None
            raise error
        self.spaces += 1

    def collect_transcript(self):
        self.collected += 1
        if FakeSession.collect_gate is not None:
            while not FakeSession.collect_gate.wait(0.01):
                if self.stopped.is_set():
                    raise DictationError("stopped")
        if FakeSession.collect_error is not None:
            error, FakeSession.collect_error = FakeSession.collect_error, None
            raise error
        return FakeSession.transcript

    def cancel_recording(self):
        self.cancelled += 1

    def stop(self):
        self.running = False
        self.stopped.set()


class FakeListener:
    instances: ClassVar[list["FakeListener"]] = []
    warning = ""

    def __init__(self, listener_settings, on_down, on_up, on_lost):
        self.settings = listener_settings
        self.on_down, self.on_up, self.on_lost = on_down, on_up, on_lost
        self.held: frozenset[str] = frozenset()
        self.stopped = False
        FakeListener.instances.append(self)

    def start(self):
        pass

    def stop(self):
        if not self.stopped:
            self.stopped = True
            self.on_lost()

    def is_alive(self):
        return not self.stopped

    def held_keys(self):
        return self.held


class ControllerCase(unittest.TestCase):
    prewarm = False
    mode = "hold"

    def setUp(self):
        FakeSession.instances = []
        FakeSession.transcript = "hello"
        FakeSession.start_gate = FakeSession.collect_gate = None
        FakeSession.collect_error = FakeSession.space_error = None
        FakeListener.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "run").mkdir()
        self.deliver = Mock(return_value=output.Delivery(copied=True, inserted=True))
        self.mute = Mock()
        self.restore = Mock()
        patches = [
            patch.dict(
                os.environ,
                {
                    "HOME": str(root),
                    "XDG_RUNTIME_DIR": str(root / "run"),
                    "XDG_DATA_HOME": str(root / "data"),
                    "XDG_CONFIG_HOME": str(root / "config"),
                },
            ),
            patch.object(controller_module, "ClaudeVoiceSession", FakeSession),
            patch.object(controller_module, "find_claude", return_value="/fake/claude"),
            patch.object(controller_module, "claude_version", return_value="9.9.9"),
            patch.object(controller_module.feedback, "play_sound"),
            patch.object(controller_module.feedback, "Banner", Mock()),
            patch.object(controller_module.system_audio, "mute_output", self.mute),
            patch.object(controller_module.system_audio, "restore_output", self.restore),
            patch.object(controller_module.output, "deliver", self.deliver),
            patch.object(controller_module.output, "focused_app", return_value=None),
            patch.object(controller_module.output, "setup_warnings", return_value=[]),
            patch.object(controller_module.hotkeys, "TriggerListener", FakeListener),
            patch.object(controller_module.hotkeys, "shortcut_problem", return_value=None),
            patch.object(controller_module.hotkeys, "describe_shortcut", return_value="F8"),
        ]
        if settings.IS_MAC:
            patches += [
                patch.object(controller_module.mac_events, "request_microphone_if_unasked"),
                patch.object(controller_module.mac_events, "can_listen", return_value=True),
                patch.object(controller_module.mac_events, "can_type", return_value=True),
            ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.settings = dict(
            settings.DEFAULT_SETTINGS, prewarm=self.prewarm, mode=self.mode, sounds=False
        )
        self.controller = controller_module.DictationController(self.settings)
        self.controller.start()
        self.addCleanup(self.controller.shutdown)
        self.listener = FakeListener.instances[-1]

    @property
    def session(self) -> FakeSession:
        return FakeSession.instances[-1]

    def total_spaces(self) -> int:
        return sum(session.spaces for session in FakeSession.instances)

    def press(self):
        self._spaces_at_press = self.total_spaces()
        self.listener.on_down()

    def release(self):
        self.listener.on_up()

    def wait_recording(self):
        self.assertTrue(
            wait_until(lambda: self.total_spaces() > self._spaces_at_press), "no Spaces"
        )

    def wait_idle(self):
        self.assertTrue(
            wait_until(lambda: self.controller._phase == "idle" and self.controller.events.empty()),
            f"still {self.controller.state}",
        )

    def spaces_stop(self):
        before = self.session.spaces
        time.sleep(0.1)
        return self.session.spaces == before

    def dictate(self):
        self.press()
        self.wait_recording()
        self.release()
        self.wait_idle()


class NormalFlowTest(ControllerCase):
    def test_press_records_release_delivers_once(self):
        self.dictate()
        self.deliver.assert_called_once()
        self.assertEqual(self.deliver.call_args.args[0], "hello")
        self.assertEqual(self.controller.state, "idle")
        self.assertEqual(self.controller.history.recent(5)[0]["text"], "hello")

    def test_spaces_start_before_slow_feedback(self):
        self.dictate()  # the session is running, as it is after prewarming
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.controller.banner.show.side_effect = lambda _title: gate.wait(WAIT_S)
        self.press()
        self.wait_recording()
        self.release()
        gate.set()
        self.wait_idle()

    def test_quick_press_never_leaves_the_speakers_muted(self):
        order = []
        self.mute.side_effect = lambda: order.append("mute")
        self.restore.side_effect = lambda: order.append("restore")
        self.dictate()
        self.assertEqual(order[-1], "restore")

    def test_mute_after_a_finished_recording_is_skipped(self):
        over = threading.Event()
        over.set()
        self.controller._mute_while(over)
        self.mute.assert_not_called()


class PauseTest(ControllerCase):
    mode = "toggle"

    def test_pause_stops_a_toggle_recording_and_stays_paused(self):
        self.press()
        self.wait_recording()
        self.release()
        self.controller.set_paused(True)
        self.assertTrue(wait_until(lambda: self.session.cancelled == 1))
        self.assertTrue(self.spaces_stop())
        self.wait_idle()
        self.deliver.assert_not_called()
        self.assertEqual(self.controller.state, "paused")
        self.assertTrue(self.restore.called)

    def test_shortcut_is_ignored_while_paused_and_works_after(self):
        self.controller.set_paused(True)
        self.press()
        self.release()
        time.sleep(0.1)
        self.assertEqual(FakeSession.instances, [])
        self.controller.set_paused(False)
        self.assertEqual(self.controller.state, "idle")
        self.press()
        self.wait_recording()
        self.press()  # toggle: second press stops
        self.wait_idle()
        self.deliver.assert_called_once()

    def test_pause_during_cold_start_never_records(self):
        FakeSession.start_gate = threading.Event()
        self.press()
        self.release()
        self.assertTrue(wait_until(lambda: FakeSession.instances))
        self.controller.set_paused(True)
        FakeSession.start_gate.set()
        self.wait_idle()
        self.assertEqual(self.session.spaces, 0)
        self.assertEqual(self.controller.state, "paused")

    def test_pause_during_transcription_keeps_the_text_and_does_not_type(self):
        FakeSession.collect_gate = threading.Event()
        self.press()
        self.wait_recording()
        self.press()
        self.assertTrue(wait_until(lambda: self.session.collected == 1))
        self.controller.set_paused(True)
        FakeSession.collect_gate.set()
        self.wait_idle()
        self.deliver.assert_not_called()
        self.assertEqual(self.controller.recovery[0]["text"], "hello")
        self.assertEqual(self.controller.state, "paused")

    def test_pause_during_delivery_signals_the_typing_to_stop(self):
        typing = threading.Event()

        def slow_deliver(_text, _settings, _target, cancel):
            typing.set()
            self.assertTrue(cancel.wait(WAIT_S))
            return output.Delivery(uncertain=True, problem="Could not insert it.")

        self.deliver.side_effect = slow_deliver
        self.press()
        self.wait_recording()
        self.press()
        self.assertTrue(typing.wait(WAIT_S))
        self.controller.set_paused(True)
        self.wait_idle()
        self.assertEqual(self.deliver.call_count, 1)  # never typed a second time
        self.assertEqual(self.controller.recovery[0]["text"], "hello")
        self.assertEqual(self.controller.state, "paused")

    def test_late_announcer_does_not_overwrite_the_state(self):
        self.controller.set_paused(True)
        over = threading.Event()
        self.controller._announce_recording(self.settings, True, over)
        self.assertEqual(self.controller.state, "paused")


class InputLostTest(ControllerCase):
    def test_losing_the_listener_mid_hold_stops_and_keeps_text_untyped(self):
        self.press()
        self.wait_recording()
        self.listener.on_lost()
        self.assertTrue(wait_until(lambda: self.session.collected == 1))
        self.wait_idle()
        self.assertTrue(self.spaces_stop())
        self.deliver.assert_not_called()
        self.assertEqual(self.controller.recovery[0]["text"], "hello")

    def test_next_press_after_a_loss_works_and_reconnect_recreates_nothing(self):
        self.press()
        self.wait_recording()
        self.listener.on_lost()
        self.wait_idle()
        count = self.session.spaces
        self.controller._start_listener()  # reconnect, key possibly still down
        self.listener = FakeListener.instances[-1]
        time.sleep(0.1)
        self.assertEqual(self.session.spaces, count)
        self.dictate()
        self.deliver.assert_called_once()

    def test_loss_during_cold_start_prevents_the_recording(self):
        FakeSession.start_gate = threading.Event()
        self.press()
        self.assertTrue(wait_until(lambda: FakeSession.instances))
        self.listener.on_lost()
        FakeSession.start_gate.set()
        self.wait_idle()
        self.assertEqual(self.session.spaces, 0)

    def test_release_and_loss_together_deliver_at_most_once(self):
        self.press()
        self.wait_recording()
        self.release()
        self.listener.on_lost()
        self.listener.on_lost()
        self.wait_idle()
        self.assertEqual(self.session.collected, 1)
        self.assertLessEqual(self.deliver.call_count, 1)

    def test_stopping_the_listener_mid_recording_cancels_it(self):
        self.press()
        self.wait_recording()
        self.controller._stop_listener()
        self.assertTrue(wait_until(lambda: self.session.collected == 1))
        self.wait_idle()
        self.deliver.assert_not_called()


class ToggleLossTest(ControllerCase):
    mode = "toggle"

    def test_loss_during_a_toggle_recording_ends_it(self):
        self.press()
        self.release()
        self.wait_recording()
        self.listener.on_lost()
        self.assertTrue(wait_until(lambda: self.session.collected == 1))
        self.wait_idle()
        self.assertTrue(self.spaces_stop())


class StaleGestureTest(ControllerCase):
    def test_press_and_release_while_transcribing_never_records_later(self):
        FakeSession.collect_gate = threading.Event()
        self.press()
        self.wait_recording()
        self.release()
        self.assertTrue(wait_until(lambda: self.session.collected == 1))
        count = self.session.spaces
        self.press()
        self.release()
        self.press()
        self.release()
        FakeSession.collect_gate.set()
        self.wait_idle()
        time.sleep(0.1)
        self.assertEqual(self.session.spaces, count)
        self.deliver.assert_called_once()

    def test_key_held_across_completion_does_not_start_a_recording(self):
        FakeSession.collect_gate = threading.Event()
        self.press()
        self.wait_recording()
        self.release()
        self.assertTrue(wait_until(lambda: self.session.collected == 1))
        count = self.session.spaces
        self.press()  # still down when the transcript finishes
        FakeSession.collect_gate.set()
        self.wait_idle()
        time.sleep(0.1)
        self.assertEqual(self.session.spaces, count)
        self.release()
        self.dictate()  # a fresh press works
        self.assertEqual(self.deliver.call_count, 2)

    def test_release_during_slow_startup_prevents_capture(self):
        FakeSession.start_gate = threading.Event()
        self.press()
        self.assertTrue(wait_until(lambda: FakeSession.instances))
        self.release()
        FakeSession.start_gate.set()
        self.wait_idle()
        self.assertEqual(self.session.spaces, 0)
        self.deliver.assert_not_called()

    def test_press_again_during_slow_startup_records_the_new_press(self):
        FakeSession.start_gate = threading.Event()
        self.press()
        self.assertTrue(wait_until(lambda: FakeSession.instances))
        self.release()
        self.press()  # still held when Claude Code becomes ready
        FakeSession.start_gate.set()
        self.wait_recording()
        self.release()
        self.wait_idle()
        self.deliver.assert_called_once()

    def test_held_press_during_prewarm_records_once_ready(self):
        FakeSession.start_gate = threading.Event()
        self.controller.events.put(controller_module.Event(controller_module.PREWARM))
        self.assertTrue(wait_until(lambda: FakeSession.instances))
        self.press()
        time.sleep(0.05)
        self.assertEqual(self.session.spaces, 0)
        FakeSession.start_gate.set()
        self.wait_recording()
        self.release()
        self.wait_idle()
        self.deliver.assert_called_once()
        self.assertEqual(len(FakeSession.instances), 1)


class ToggleGestureTest(ControllerCase):
    mode = "toggle"

    def test_release_is_ignored_and_second_press_stops(self):
        self.press()
        self.release()
        self.wait_recording()
        time.sleep(0.1)
        self.assertEqual(self.session.collected, 0)
        self.press()
        self.release()
        self.wait_idle()
        self.deliver.assert_called_once()

    def test_two_presses_during_cold_start_mean_start_then_stop(self):
        FakeSession.start_gate = threading.Event()
        self.press()
        self.assertTrue(wait_until(lambda: FakeSession.instances))
        self.release()
        self.press()
        self.release()
        FakeSession.start_gate.set()
        self.wait_idle()
        self.assertEqual(self.session.spaces, 0)

    def test_press_while_transcribing_does_not_start_another_toggle_recording(self):
        FakeSession.collect_gate = threading.Event()
        self.press()
        self.wait_recording()
        self.press()
        self.assertTrue(wait_until(lambda: self.session.collected == 1))
        count = self.session.spaces
        self.press()
        self.release()
        FakeSession.collect_gate.set()
        self.wait_idle()
        time.sleep(0.1)
        self.assertEqual(self.session.spaces, count)


class ToggleDuringPrewarmTest(ControllerCase):
    mode = "toggle"

    def test_start_then_stop_during_prewarm_never_records(self):
        FakeSession.start_gate = threading.Event()
        self.controller.events.put(controller_module.Event(controller_module.PREWARM))
        self.assertTrue(wait_until(lambda: FakeSession.instances))
        self.press()
        self.release()
        self.press()
        self.release()
        FakeSession.start_gate.set()
        self.wait_idle()
        time.sleep(0.1)
        self.assertEqual(self.session.spaces, 0)
        self.assertEqual(self.controller.state, "idle")


class ListenerFailureTest(ControllerCase):
    def failing_listener(self):
        class EvdevStyleError(Exception):  # evdev's UInputError derives from Exception only
            pass

        class Failing(FakeListener):
            def start(self):
                raise EvdevStyleError("/dev/uinput cannot be opened for writing")

        return patch.object(controller_module.hotkeys, "TriggerListener", Failing)

    def test_listener_failure_is_reported_and_keeps_the_app_alive(self):
        with (
            self.failing_listener(),
            self.assertLogs("claude_dictation_anywhere.controller", "ERROR"),
        ):
            self.controller.update_settings({"grab": True})
        self.assertIn("uinput", self.controller.listener_error)
        self.assertIsNone(self.controller.listener)
        self.assertIn("uinput", self.controller.status()["warnings"][0]["text"])

    def test_it_is_retried_later(self):
        with (
            self.failing_listener(),
            self.assertLogs("claude_dictation_anywhere.controller", "ERROR"),
        ):
            self.controller._start_listener()
        self.controller._listener_tried_at = -controller_module.LISTENER_RETRY_INTERVAL_S * 2
        self.controller.status()
        self.assertEqual(self.controller.listener_error, "")
        self.assertIsNotNone(self.controller.listener)

    def test_failure_in_housekeeping_does_not_kill_the_worker(self):
        self.controller.settings = {**self.controller.settings, "idle_shutdown_minutes": 1}
        with (
            patch.object(self.controller, "_housekeeping", side_effect=KeyError("bug")),
            patch.object(controller_module, "IDLE_CHECK_INTERVAL_S", 0.01),
            self.assertLogs("claude_dictation_anywhere.controller", "ERROR"),
        ):
            self.controller.events.put(controller_module.Event(controller_module.WAKE))
            self.assertTrue(wait_until(lambda: self.controller._housekeeping.called))
            time.sleep(0.05)
        self.assertTrue(self.controller._worker_thread.is_alive())


class SettingsDuringRecordingTest(ControllerCase):
    def test_language_change_during_a_hold_applies_to_the_next_recording(self):
        self.press()
        self.wait_recording()
        first = self.session
        self.controller.update_settings({"language": "fr"})
        self.assertTrue(first.running)  # the active recording keeps its session
        self.release()
        self.wait_idle()
        self.assertFalse(first.running)
        self.assertEqual(self.controller.settings["language"], "fr")
        self.press()
        self.wait_recording()
        self.assertEqual(self.session.language, "fr")
        self.release()
        self.wait_idle()

    def test_language_change_while_transcribing_is_not_lost(self):
        FakeSession.collect_gate = threading.Event()
        self.press()
        self.wait_recording()
        self.release()
        self.assertTrue(wait_until(lambda: self.session.collected == 1))
        self.controller.update_settings({"language": "de"})
        FakeSession.collect_gate.set()
        self.wait_idle()
        self.dictate()
        self.assertEqual(self.session.language, "de")

    def test_mode_change_during_a_recording_does_not_change_that_recording(self):
        self.press()
        self.wait_recording()
        self.controller.update_settings({"mode": "toggle"})
        self.release()  # this recording is still hold mode
        self.wait_idle()
        self.deliver.assert_called_once()

    def test_unusable_program_path_is_saved_and_reported_without_crashing(self):
        with patch.object(controller_module, "find_claude", return_value=None):
            self.controller.update_settings({"claude_path": "~nobody-here/claude"})
        self.assertEqual(self.controller.claude_path, "")
        self.assertIn("not found", self.controller.last_error)

    def test_program_path_change_replaces_the_session(self):
        self.dictate()
        first = self.session
        with patch.object(controller_module, "find_claude", return_value="/other/claude"):
            self.controller.update_settings({"claude_path": "/other/claude"})
        self.wait_idle()
        self.assertFalse(first.running)
        self.dictate()
        self.assertEqual(self.session.claude_path, "/other/claude")


class WorkerSurvivalTest(ControllerCase):
    def assert_recovers(self):
        self.assertEqual(self.controller.state, "idle")
        self.assertTrue(self.controller._worker_thread.is_alive())
        self.deliver.reset_mock()
        self.dictate()
        self.deliver.assert_called_once()

    def test_terminal_failure_during_a_hold(self):
        FakeSession.space_error = OSError("closed pty")
        self.press()
        self.assertTrue(wait_until(lambda: self.controller.last_error))
        self.wait_idle()
        self.assertNotIn("closed pty", self.controller.last_error)
        self.release()
        self.assert_recovers()

    def test_failures_during_collection(self):
        for error in (OSError("read failed"), UnicodeDecodeError("utf-8", b"x", 0, 1, "bad")):
            with self.subTest(type(error).__name__):
                FakeSession.collect_error = error
                self.press()
                self.wait_recording()
                self.release()
                self.assertTrue(wait_until(lambda: self.controller.last_error))
                self.wait_idle()
                self.assertFalse(self.session.running)  # an unusable session is replaced
                self.controller.last_error = ""
                self.assert_recovers()

    def test_a_bug_is_logged_and_dictation_lives_on(self):
        FakeSession.collect_error = KeyError("bug")
        with self.assertLogs("claude_dictation_anywhere.controller", "ERROR"):
            self.press()
            self.wait_recording()
            self.release()
            self.assertTrue(wait_until(lambda: self.controller.last_error))
            self.wait_idle()
        self.assert_recovers()

    def test_partial_transcript_is_offered_not_typed(self):
        FakeSession.collect_error = PartialTranscript("It stopped.", "half a sentence")
        self.press()
        self.wait_recording()
        self.release()
        self.assertTrue(wait_until(lambda: self.controller.recovery))
        self.wait_idle()
        self.deliver.assert_not_called()
        self.assertEqual(self.controller.recovery[0]["text"], "half a sentence")
        self.assertNotIn("half a sentence", self.controller.last_error)
        self.assert_recovers()

    def test_history_failure_does_not_stop_delivery(self):
        with patch.object(self.controller.history, "add", side_effect=OSError(28, "No space")):
            self.dictate()
        self.deliver.assert_called_once()
        self.assertIn("history", self.controller.last_error)

    def test_delivery_failure_keeps_the_text_in_memory_even_with_history_off(self):
        self.controller.update_settings({"history": False})
        self.deliver.return_value = output.Delivery(problem="Could not insert it.")
        self.dictate()
        self.assertEqual(self.controller.recovery[0]["text"], "hello")
        self.assertEqual(self.controller.history.recent(5), [])
        self.deliver.return_value = output.Delivery(inserted=True)
        self.controller.last_error = ""
        self.assert_recovers()

    def test_output_none_with_everything_off_still_keeps_the_text(self):
        self.controller.update_settings(
            {"history": False, "copy_to_clipboard": False, "output": "none"}
        )
        self.deliver.return_value = output.Delivery()
        self.dictate()
        self.assertEqual(self.controller.recovery[0]["text"], "hello")

    def test_recovery_can_be_dismissed_and_is_bounded(self):
        for index in range(controller_module.MAX_RECOVERY_ENTRIES + 2):
            self.controller._keep_for_recovery(f"text {index}", "reason")
        self.assertEqual(len(self.controller.recovery), controller_module.MAX_RECOVERY_ENTRIES)
        newest = self.controller.recovery[0]
        self.assertEqual(newest["text"], f"text {controller_module.MAX_RECOVERY_ENTRIES + 1}")
        self.controller.clear_recovery(newest["id"])
        self.assertNotIn(newest, self.controller.recovery)

    def test_text_is_not_typed_while_the_shortcut_is_still_held(self):
        self.listener.held = frozenset({"ctrl"})
        with patch.object(controller_module, "RELEASE_WAIT_S", 0.05):
            self.dictate()
        self.deliver.assert_not_called()
        self.assertEqual(self.controller.recovery[0]["text"], "hello")

    def test_text_waits_for_a_key_released_just_after_the_stop(self):
        self.listener.held = frozenset({"ctrl"})
        threading.Timer(0.1, lambda: setattr(self.listener, "held", frozenset())).start()
        self.dictate()
        self.deliver.assert_called_once()

    def test_dead_worker_is_never_shown_as_ready(self):
        thread = Mock()
        thread.is_alive.return_value = False
        with patch.object(self.controller, "_worker_thread", thread):
            self.assertEqual(self.controller.state, "failed")


class ShutdownTest(ControllerCase):
    def assert_cleanly_stopped(self):
        self.assertFalse(self.controller._worker_thread.is_alive())
        self.assertTrue(self.restore.called)
        self.assertTrue(self.listener.stopped)
        if FakeSession.instances:
            self.assertFalse(self.session.running)
        self.controller.banner.close.assert_called()
        self.controller.shutdown()  # repeated: nothing happens, nothing raises

    def test_idle(self):
        self.controller.shutdown()
        self.assert_cleanly_stopped()

    def test_while_recording(self):
        self.press()
        self.wait_recording()
        self.controller.shutdown()
        self.assertTrue(self.spaces_stop())
        self.deliver.assert_not_called()
        self.assert_cleanly_stopped()

    def test_while_starting(self):
        FakeSession.start_gate = threading.Event()
        self.addCleanup(FakeSession.start_gate.set)
        self.press()
        self.assertTrue(wait_until(lambda: FakeSession.instances))
        began = time.monotonic()
        self.controller.shutdown()
        self.assertLess(time.monotonic() - began, WAIT_S)
        self.assert_cleanly_stopped()

    def test_while_transcribing(self):
        FakeSession.collect_gate = threading.Event()
        self.addCleanup(FakeSession.collect_gate.set)
        self.press()
        self.wait_recording()
        self.release()
        self.assertTrue(wait_until(lambda: self.session.collected == 1))
        self.controller.shutdown()
        self.deliver.assert_not_called()
        self.assert_cleanly_stopped()

    def test_while_delivering(self):
        typing = threading.Event()

        def slow_deliver(_text, _settings, _target, cancel):
            typing.set()
            cancel.wait(WAIT_S)
            return output.Delivery(uncertain=True, problem="Stopped.")

        self.deliver.side_effect = slow_deliver
        self.press()
        self.wait_recording()
        self.release()
        self.assertTrue(typing.wait(WAIT_S))
        began = time.monotonic()
        self.controller.shutdown()
        self.assertLess(time.monotonic() - began, 2)
        self.assert_cleanly_stopped()

    def test_one_failing_step_does_not_skip_the_rest(self):
        self.restore.side_effect = RuntimeError("mixer broke")
        self.press()
        self.wait_recording()
        with self.assertLogs("claude_dictation_anywhere.controller", "ERROR"):
            self.controller.shutdown()
        self.assertFalse(self.session.running)
        self.controller.banner.close.assert_called()

    def test_shutdown_without_start_is_safe(self):
        controller = controller_module.DictationController(self.settings)
        controller.shutdown()
        controller.shutdown()


class SettingsTransactionTest(ControllerCase):
    def test_failed_save_changes_nothing(self):
        before = dict(self.controller.settings)
        with (
            patch.object(controller_module, "save_settings", side_effect=PermissionError(13, "no")),
            self.assertRaises(OSError),
        ):
            self.controller.update_settings({"mode": "toggle"})
        self.assertEqual(self.controller.settings, before)
        self.assertFalse(settings.settings_path().exists())

    def test_saved_file_and_memory_agree(self):
        self.controller.update_settings({"mode": "toggle"})
        self.assertEqual(settings.load_settings()["mode"], "toggle")
        self.assertEqual(self.controller.settings["mode"], "toggle")

    def test_refused_shortcut_changes_nothing(self):
        controller_module.hotkeys.shortcut_problem.return_value = "That types text."
        with self.assertRaisesRegex(ValueError, "types text"):
            self.controller.update_settings({"shortcut_key": "KEY_A"})
        self.assertEqual(self.controller.settings["shortcut_key"], self.settings["shortcut_key"])

    def test_invalid_shortcut_in_the_file_is_reported_not_run(self):
        controller_module.hotkeys.shortcut_problem.return_value = "Unknown key code."
        count = len(FakeListener.instances)
        self.controller._start_listener()
        self.assertEqual(len(FakeListener.instances), count)
        self.assertIn("Unknown key code", self.controller.listener_error)

    def test_capture_saves_through_the_same_transaction(self):
        with patch.object(
            controller_module.hotkeys, "capture_next_input", return_value=("KEY_F9", "")
        ):
            self.controller.capture_shortcut()
            self.controller._capture_thread.join(WAIT_S)
        self.assertEqual(self.controller.capture["status"], "done")
        self.assertEqual(settings.load_settings()["shortcut_key"], "KEY_F9")
        self.assertEqual(self.controller.state, "idle")
        self.assertFalse(FakeListener.instances[-1].stopped)

    def test_failed_capture_save_leaves_old_shortcut_and_a_listener(self):
        before = self.controller.settings["shortcut_key"]
        with (
            patch.object(
                controller_module.hotkeys, "capture_next_input", return_value=("KEY_F9", "")
            ),
            patch.object(controller_module, "save_settings", side_effect=OSError(28, "No space")),
        ):
            self.controller.capture_shortcut()
            self.controller._capture_thread.join(WAIT_S)
        self.assertEqual(self.controller.capture["status"], "rejected")
        self.assertIn("No space", self.controller.capture["message"])
        self.assertEqual(self.controller.settings["shortcut_key"], before)
        self.assertFalse(FakeListener.instances[-1].stopped)
        self.assertEqual(self.controller.state, "idle")

    def test_capture_that_raises_still_restores_a_listener(self):
        with patch.object(
            controller_module.hotkeys, "capture_next_input", side_effect=KeyError("bug")
        ):
            self.controller.capture_shortcut()
            self.controller._capture_thread.join(WAIT_S)
        self.assertEqual(self.controller.capture["status"], "rejected")
        self.assertFalse(FakeListener.instances[-1].stopped)

    def test_pause_during_capture_is_kept_after_it(self):
        with patch.object(controller_module.hotkeys, "capture_next_input", return_value=None):
            self.controller.set_paused(True)
            self.controller.capture_shortcut()
            self.controller._capture_thread.join(WAIT_S)
        self.assertEqual(self.controller.state, "paused")

    def test_capture_is_refused_while_recording(self):
        self.press()
        self.wait_recording()
        self.controller._phase = "listening"
        self.controller.capture_shortcut()
        self.assertEqual(self.controller.capture["status"], "rejected")
        self.release()
        self.wait_idle()

    def test_concurrent_settings_changes_all_land(self):
        errors = []

        def change(update):
            try:
                self.controller.update_settings(update)
            except Exception as error:  # noqa: BLE001 - the point of this test
                errors.append(error)

        threads = [
            threading.Thread(target=change, args=({name: value},))
            for name, value in (
                ("sounds", True),
                ("notifications", False),
                ("mode", "toggle"),
                ("copy_to_clipboard", False),
            )
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        saved = settings.load_settings()
        self.assertTrue(saved["sounds"])
        self.assertFalse(saved["notifications"])
        self.assertEqual(saved["mode"], "toggle")
        self.assertFalse(saved["copy_to_clipboard"])


class StatusTest(ControllerCase):
    def test_status_does_not_reap_or_read_slowly(self):
        self.dictate()
        with patch.object(self.session, "is_alive", side_effect=AssertionError("reaped")):
            self.assertTrue(self.controller.status()["session_running"])

    def test_status_returns_copies_and_shows_settings_problems(self):
        controller = controller_module.DictationController(self.settings, ["Bad file."])
        status = controller.status()
        status["settings"]["mode"] = "changed"
        self.assertEqual(controller.settings["mode"], self.settings["mode"])
        self.assertIn("Bad file.", [warning["text"] for warning in status["warnings"]])

    def test_setup_warnings_are_cached_between_polls(self):
        checker = controller_module.output.setup_warnings
        self.controller._warnings_cache = (-controller_module.WARNINGS_CACHE_S, [])
        checker.reset_mock()
        self.controller.status()
        self.controller.status()
        checker.assert_called_once()


if __name__ == "__main__":
    unittest.main()
