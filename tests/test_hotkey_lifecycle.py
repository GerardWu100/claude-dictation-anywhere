"""Exercise Linux listener cleanup without accessing real input devices."""

import importlib.util
import os
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
        ecodes = types.SimpleNamespace(EV_KEY=1, ecodes={"KEY_F8": 66})
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
        )

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
