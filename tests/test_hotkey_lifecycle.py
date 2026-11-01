"""Exercise Linux listener cleanup without accessing real input devices."""

import importlib.util
import os
import select
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from claude_dictation_anywhere import settings


class FakeDevice:
    def __init__(self, keys):
        self.fd, self.write_fd = os.pipe()
        self.keys = keys
        self.name = "test keyboard"
        self.path = "test-device"
        self.closed = False
        self.grab = Mock()
        self.held_now = []

    def active_keys(self):
        return self.held_now

    def capabilities(self):
        return {1: self.keys}

    def fileno(self):
        return self.fd

    def close(self):
        if not self.closed:
            os.close(self.fd)
            os.close(self.write_fd)
            self.closed = True


class LinuxListenerLifecycleTest(unittest.TestCase):
    def setUp(self):
        ecodes = types.SimpleNamespace(
            EV_KEY=1,
            EV_SYN=0,
            SYN_REPORT=0,
            SYN_DROPPED=3,
            BTN_LEFT=272,
            BTN_RIGHT=273,
            BTN_TOUCH=330,
            BTN_TOOL_FINGER=325,
            BTN_TOOL_DOUBLETAP=333,
            BTN_TOOL_TRIPLETAP=334,
            BTN_TOOL_QUADTAP=335,
            BTN_TOOL_QUINTTAP=328,
            BTN={},
            KEY={66: "KEY_F8"},
            ecodes={"KEY_F8": 66},
        )
        self.evdev = types.SimpleNamespace(ecodes=ecodes, UInput=Mock())
        module_path = Path(settings.__file__).with_name("hotkeys.py")
        spec = importlib.util.spec_from_file_location(
            "claude_dictation_anywhere._test_linux_hotkeys", module_path
        )
        self.module = importlib.util.module_from_spec(spec)
        with (
            patch.object(settings, "IS_MAC", False),
            patch.dict("sys.modules", {"evdev": self.evdev}),
        ):
            spec.loader.exec_module(self.module)
        self.devices = []

    def tearDown(self):
        for device in self.devices:
            device.close()

    def device(self, keys):
        device = FakeDevice(keys)
        self.devices.append(device)
        return device

    def listener(self, grab=False):
        return self.module.TriggerListener(
            {"shortcut_key": "KEY_F8", "shortcut_device": "", "grab": grab},
            Mock(),
            Mock(),
            Mock(),
        )

    def feed(self, device, *batches):
        """Make each batch of (type, code, value) events come back from one device.read()."""
        pending = list(batches)

        def read():
            os.read(device.fd, 1)
            return [
                types.SimpleNamespace(type=kind, code=code, value=value)
                for kind, code, value in pending.pop(0)
            ]

        device.read = read
        for _ in batches:
            os.write(device.write_fd, b"x")

    def started(self, device, grab=False):
        listener = self.listener(grab=grab)
        with patch.object(self.module, "_real_devices", return_value=[device]):
            listener.start()
        return listener

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(condition())

    def test_unplug_during_a_hold_reports_lost_and_no_release(self):
        device = self.device([66])
        listener = self.started(device)
        reads = iter([[types.SimpleNamespace(type=1, code=66, value=1)]])

        def read():
            os.read(device.fd, 1)
            try:
                return next(reads)
            except StopIteration:
                raise OSError(19, "No such device") from None

        device.read = read
        os.write(device.write_fd, b"x")
        self.wait_for(lambda: listener.on_down.called)
        self.assertEqual(listener.held_keys(), {"KEY_F8"})
        os.write(device.write_fd, b"x")
        self.wait_for(lambda: listener.on_lost.called)
        listener.stop()
        listener.on_up.assert_not_called()
        listener.on_lost.assert_called_once()
        self.assertEqual(listener.held_keys(), frozenset())
        self.assertFalse(listener.is_alive())
        self.assertTrue(device.closed)

    def test_stop_during_a_hold_reports_lost_once(self):
        device = self.device([66])
        listener = self.started(device)
        self.feed(device, [(1, 66, 1)])
        self.wait_for(lambda: listener.on_down.called)
        listener.stop()
        listener.stop()
        listener.on_lost.assert_called_once()
        listener.on_up.assert_not_called()

    def test_a_new_listener_does_not_treat_a_key_held_across_reconnect_as_a_press(self):
        device = self.device([66])
        listener = self.started(device)
        self.feed(device, [(1, 66, 2)], [(1, 66, 0)])  # auto-repeat, then the release
        self.wait_for(lambda: not self.device_has_pending(device))
        listener.stop()
        listener.on_down.assert_not_called()
        listener.on_up.assert_not_called()

    def device_has_pending(self, device):
        return bool(select.select([device.fd], [], [], 0)[0])

    def test_dropped_events_cancel_the_gesture_and_are_skipped_until_the_next_report(self):
        device = self.device([66])
        listener = self.started(device)
        self.feed(
            device,
            [(1, 66, 1)],
            [(0, 3, 0), (1, 66, 0), (0, 0, 0)],  # dropped; a release in the torn part; report
        )
        self.wait_for(lambda: listener.on_lost.called)
        self.wait_for(lambda: not self.device_has_pending(device))
        time.sleep(0.02)
        self.assertTrue(listener.is_alive())  # the device still works
        listener.on_up.assert_not_called()
        self.assertEqual(listener.held_keys(), frozenset())
        listener.stop()

    def test_keys_replayed_on_the_virtual_device_are_let_go_when_it_closes(self):
        self.evdev.ecodes.ecodes.update(KEY_A=30)
        twin = Mock()
        self.evdev.UInput.from_device.return_value = twin
        device = self.device([66, 30])
        listener = self.started(device, grab=True)
        self.feed(device, [(1, 30, 1)])  # A goes down and is replayed
        self.wait_for(lambda: twin.write_event.called)
        listener.stop()
        twin.write.assert_called_once_with(1, 30, 0)
        twin.syn.assert_called_once()
        twin.close.assert_called_once()
        self.assertTrue(device.closed)

    def test_stop_closes_devices_and_pipes_and_is_idempotent(self):
        selected, excluded = self.device([66]), self.device([1])
        listener = self.listener()
        pipe_fds = listener._wake_read, listener._wake_write
        with patch.object(self.module, "_real_devices", return_value=[selected, excluded]):
            listener.start()
        listener.stop()
        listener.stop()
        self.assertFalse(listener._thread.is_alive())
        self.assertTrue(all(device.closed for device in self.devices))
        for fd in pipe_fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_setup_failure_releases_earlier_grab_and_all_resources(self):
        first, second = self.device([66]), self.device([66])
        twin = Mock()
        self.evdev.UInput.from_device.side_effect = [twin, PermissionError("uinput denied")]
        listener = self.listener(grab=True)
        with (
            patch.object(self.module, "_real_devices", return_value=[first, second]),
            self.assertRaises(PermissionError),
        ):
            listener.start()
        first.grab.assert_called_once()
        twin.close.assert_called_once()
        self.assertTrue(first.closed and second.closed)
        self.assertIsNone(listener._wake_read)
        listener.stop()

    def test_grabbed_combination_replays_modifiers_and_hides_the_other_key(self):
        self.evdev.ecodes.ecodes.update(KEY_LEFTCTRL=29)
        twin = Mock()
        self.evdev.UInput.from_device.return_value = twin
        on_down, on_up = Mock(), Mock()
        listener = self.module.TriggerListener(
            {"shortcut_key": "KEY_LEFTCTRL+KEY_F8", "shortcut_device": "", "grab": True},
            on_down,
            on_up,
            Mock(),
        )
        # (code, value): F8 alone, then Ctrl down, F8 down / repeat / up, Ctrl up.
        presses = [(66, 1), (66, 0), (29, 1), (66, 1), (66, 2), (66, 0), (29, 0)]
        device = self.device([29, 66])

        def read():
            os.read(device.fd, 1)  # drain, so the device reads once
            return [types.SimpleNamespace(type=1, code=c, value=v) for c, v in presses]

        device.read = read
        with patch.object(self.module, "_real_devices", return_value=[device]):
            listener.start()
            os.write(device.write_fd, b"x")  # make the device look readable once
            deadline = time.monotonic() + 2
            while not on_up.called and time.monotonic() < deadline:
                time.sleep(0.01)
        listener.stop()
        replayed = [(e.code, e.value) for (e,), _ in twin.write_event.call_args_list]
        self.assertEqual(replayed, [(66, 1), (66, 0), (29, 1), (29, 0)])
        on_down.assert_called_once()
        on_up.assert_called_once()

    def test_a_device_unplugged_while_listing_is_skipped(self):
        good = self.device([66])
        self.evdev.list_devices = Mock(return_value=["gone", "good"])
        self.evdev.InputDevice = Mock(side_effect=[FileNotFoundError("gone"), good])
        self.assertEqual(self.module._real_devices(), [good])

    def test_capture_goes_on_when_another_device_is_unplugged(self):
        gone, good = self.device([66]), self.device([66])
        gone.read = Mock(side_effect=OSError(19, "No such device"))
        self.feed(good, [(1, 66, 1), (1, 66, 0)])
        os.write(gone.write_fd, b"x")
        with patch.object(self.module, "_real_devices", return_value=[gone, good]):
            found = self.module.capture_next_input(2)
        self.assertEqual(found, ("KEY_F8", "test keyboard"))

    def test_warning_says_when_a_key_still_reaches_other_apps(self):
        cases = {
            ("KEY_F8", False): "",  # a lone function key usually has no job to hide
            ("KEY_LEFTCTRL+KEY_LEFTALT", False): "",  # modifiers are never hidden
            ("KEY_LEFTCTRL+KEY_G", False): "G also reaches",
            ("BTN_SIDE", False): "back button also reaches",
            ("KEY_LEFTCTRL+KEY_G", True): "",
            ("BTN_SIDE", True): "",
        }
        for (shortcut, grab), expected in cases.items():
            listener = self.module.TriggerListener(
                {"shortcut_key": shortcut, "shortcut_device": "", "grab": grab},
                Mock(),
                Mock(),
                Mock(),
            )
            self.addCleanup(listener.stop)
            with self.subTest(shortcut=shortcut, grab=grab):
                self.assertIn(expected, listener.warning)
                self.assertEqual(bool(expected), bool(listener.warning))

    def test_no_matching_device_closes_open_devices_and_pipes(self):
        device = self.device([1])
        listener = self.listener()
        with (
            patch.object(self.module, "_real_devices", return_value=[device]),
            self.assertRaises(ValueError),
        ):
            listener.start()
        self.assertTrue(device.closed)
        self.assertIsNone(listener._wake_write)
        listener.stop()
