"""Mute ownership: only the device this app muted is unmuted, whatever happens meanwhile."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from claude_dictation_anywhere import settings, system_audio


class FakeMixer:
    """Stands in for one audio tool: device states, the current default, and failures."""

    def __init__(self):
        self.muted = {"A": False, "B": True}
        self.default = "A"
        self.calls: list[tuple[str, bool]] = []
        self.set_error: Exception | None = None

    def default_output_device(self):
        return f"{system_audio.WPCTL}:{self.default}"

    def device_muted(self, device):
        return self.muted[device.partition(":")[2]]

    def set_device_muted(self, device, muted):
        if self.set_error is not None:
            raise self.set_error
        name = device.partition(":")[2]
        if name not in self.muted:
            raise subprocess.CalledProcessError(1, "wpctl")
        self.muted[name] = muted
        self.calls.append((name, muted))


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mixer = FakeMixer()
        patches = [
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp.name}),
            patch.object(system_audio, "default_output_device", self.mixer.default_output_device),
            patch.object(system_audio, "device_muted", self.mixer.device_muted),
            patch.object(system_audio, "set_device_muted", self.mixer.set_device_muted),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def record(self):
        return system_audio._record_path()

    def test_playing_output_is_muted_then_unmuted_once(self):
        system_audio.mute_output()
        self.assertTrue(self.mixer.muted["A"])
        system_audio.restore_output()
        system_audio.restore_output()
        self.assertEqual(self.mixer.calls, [("A", True), ("A", False)])
        self.assertFalse(self.record().exists())

    def test_already_muted_output_is_left_alone(self):
        self.mixer.default = "B"
        system_audio.mute_output()
        system_audio.restore_output()
        self.assertEqual(self.mixer.calls, [])
        self.assertTrue(self.mixer.muted["B"])

    def test_switching_the_default_restores_the_device_that_was_muted(self):
        system_audio.mute_output()  # A is default and playing
        self.mixer.default = "B"  # B is already muted by the user
        system_audio.restore_output()
        self.assertFalse(self.mixer.muted["A"])  # A plays again
        self.assertTrue(self.mixer.muted["B"])  # B is still the user's muted device

    def test_failed_mute_leaves_no_record_that_could_unmute_later(self):
        self.mixer.set_error = subprocess.CalledProcessError(1, "wpctl")
        system_audio.mute_output()
        self.mixer.set_error = None
        self.assertFalse(self.record().exists())
        self.mixer.muted["A"] = True  # the user mutes it afterwards
        system_audio.restore_output()
        self.assertTrue(self.mixer.muted["A"])

    def test_mute_that_timed_out_is_still_undone(self):
        self.mixer.set_error = subprocess.TimeoutExpired("wpctl", 3)
        system_audio.mute_output()  # it may have landed
        self.assertTrue(self.record().exists())
        self.mixer.set_error = None
        system_audio.restore_output()
        self.assertEqual(self.mixer.calls, [("A", False)])
        self.assertFalse(self.record().exists())

    def test_device_that_has_gone_is_forgotten(self):
        system_audio.mute_output()
        del self.mixer.muted["A"]
        system_audio.restore_output()
        self.assertFalse(self.record().exists())
        self.assertEqual(self.mixer.calls, [("A", True)])

    def test_restore_that_could_not_run_is_kept_for_later(self):
        system_audio.mute_output()
        self.mixer.set_error = OSError("no tool")
        system_audio.restore_output()
        self.assertTrue(self.record().exists())
        self.mixer.set_error = None
        system_audio.restore_output()
        self.assertFalse(self.mixer.muted["A"])
        self.assertFalse(self.record().exists())

    def test_crash_after_muting_is_undone_on_next_start(self):
        system_audio.mute_output()
        # The app dies here; a new copy has only the record.
        system_audio.restore_output()
        self.assertFalse(self.mixer.muted["A"])

    def test_crash_between_writing_the_record_and_muting_changes_nothing(self):
        system_audio.write_private_file(
            self.record(), json.dumps({"device": f"{system_audio.WPCTL}:A"})
        )
        system_audio.restore_output()
        self.assertFalse(self.mixer.muted["A"])
        self.assertFalse(self.record().exists())

    def test_damaged_records_authorise_nothing(self):
        for content in (
            "not json",
            "[]",
            "{}",
            '{"device": 5}',
            '{"device": "other-tool:A"}',
            '{"device": "coreaudio:not-a-number"}',
            b"\xff\xfe",
        ):
            with self.subTest(content):
                mode = "wb" if isinstance(content, bytes) else "w"
                with open(self.record(), mode) as file:
                    file.write(content)
                self.mixer.muted["A"] = True
                system_audio.restore_output()
                self.assertTrue(self.mixer.muted["A"])
                self.assertFalse(self.record().exists())

    def test_record_is_private(self):
        system_audio.mute_output()
        self.assertEqual(stat.S_IMODE(self.record().stat().st_mode), 0o600)

    def test_unknown_default_device_is_not_muted(self):
        with patch.object(system_audio, "default_output_device", return_value=None):
            system_audio.mute_output()
        self.assertEqual(self.mixer.calls, [])
        self.assertFalse(self.record().exists())

    def test_failing_mixer_never_raises(self):
        with patch.object(system_audio, "default_output_device", side_effect=OSError("x")):
            system_audio.mute_output()
        with patch.object(
            system_audio, "device_muted", side_effect=subprocess.TimeoutExpired("x", 1)
        ):
            system_audio.mute_output()
        self.assertFalse(self.record().exists())


class LinuxToolsTest(unittest.TestCase):
    """The exact commands for each Linux tool, with the outputs those tools print."""

    def run_with(self, tool, outputs):
        def which(name):
            return f"/usr/bin/{name}" if name == tool else None

        commands = []

        def run(args):
            commands.append(args)
            return outputs.get(args[1], "")

        with (
            patch.object(system_audio, "IS_MAC", False),
            patch.object(system_audio.shutil, "which", which),
            patch.object(system_audio, "_run", run),
        ):
            device = system_audio.default_output_device()
            muted = system_audio.device_muted(device)
            system_audio.set_device_muted(device, True)
        return device, muted, commands

    def test_wpctl_addresses_the_device_by_id(self):
        device, muted, commands = self.run_with(
            "wpctl",
            {
                "inspect": "id 57, type PipeWire:Interface:Node\n",
                "get-volume": "Volume: 0.5 [MUTED]",
            },
        )
        self.assertEqual(device, "wpctl:57")
        self.assertTrue(muted)
        self.assertEqual(commands[-1], ["wpctl", "set-mute", "57", "1"])

    def test_pactl_addresses_the_device_by_name(self):
        device, muted, commands = self.run_with(
            "pactl", {"get-default-sink": "alsa_output.pci\n", "get-sink-mute": "Mute: no"}
        )
        self.assertEqual(device, "pactl:alsa_output.pci")
        self.assertFalse(muted)
        self.assertEqual(commands[-1], ["pactl", "set-sink-mute", "alsa_output.pci", "1"])

    def test_no_tool_means_no_device(self):
        with (
            patch.object(system_audio, "IS_MAC", False),
            patch.object(system_audio.shutil, "which", return_value=None),
        ):
            self.assertIsNone(system_audio.default_output_device())


@unittest.skipUnless(settings.IS_MAC, "macOS CoreAudio")
class MacDeviceTest(unittest.TestCase):
    def test_default_device_is_read_from_coreaudio(self):
        device = system_audio.default_output_device()
        self.assertRegex(device, r"^coreaudio:\d+$")

    def test_default_device_uses_osascript_and_another_uses_coreaudio(self):
        with (
            patch.object(system_audio, "_mac_default_output_id", return_value=7),
            patch.object(system_audio, "_run") as run,
            patch.object(system_audio, "_mac_set_device_muted") as direct,
        ):
            system_audio.set_device_muted("coreaudio:7", False)
            run.assert_called_once_with(["osascript", "-e", "set volume output muted false"])
            direct.assert_not_called()
            system_audio.set_device_muted("coreaudio:9", False)
            direct.assert_called_once_with(9, False)

    def test_device_that_has_gone_raises_the_gone_error(self):
        with self.assertRaises(subprocess.CalledProcessError):
            system_audio._mac_set_device_muted(999_999, False)


if __name__ == "__main__":
    unittest.main()
