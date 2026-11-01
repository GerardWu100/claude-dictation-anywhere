"""Live checks on a real Linux desktop: real kernel input, ydotool, PipeWire, tray, notifications.

Skipped unless asked for, because they press virtual keys and briefly mute the speakers:

    CDA_LIVE_LINUX=1 uv run python -m unittest tests.test_live_linux -v

Nothing reaches the app in front. Virtual keys are F23 and F24 (no job on a normal
desktop), and ydotool's virtual device is grabbed while it types, so the keys it
sends arrive here instead. Do not type during the run: capturing a shortcut reads
every keyboard. Claude Code is the offline stand-in in fake_claude.py, so there is
no login, microphone, or network. Quit the real app first; only one copy can run.
"""

import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from claude_dictation_anywhere import feedback, output, settings, system_audio

LIVE = os.environ.get("CDA_LIVE_LINUX") == "1" and not settings.IS_MAC
if LIVE:
    import evdev
    from evdev import ecodes

    from claude_dictation_anywhere import hotkeys

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
DEVICE_NAME = "cda live check keyboard"
MOUSE_NAME = "cda live check mouse"
WAIT_S = 10
DEVICE_SETTLE_S = 0.3
QUIET_READ_S = 0.4
QUIT_LIMIT_S = 8
HOLD_S = 1.0

US_KEYS = {
    **{f"KEY_{c}": (c.lower(), c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{f"KEY_{d}": (d, d) for d in "2345678"},
    "KEY_1": ("1", "!"),
    "KEY_9": ("9", "("),
    "KEY_0": ("0", ")"),
    "KEY_SPACE": (" ", " "),
    "KEY_COMMA": (",", "<"),
    "KEY_DOT": (".", ">"),
    "KEY_APOSTROPHE": ("'", '"'),
    "KEY_MINUS": ("-", "_"),
}


def wait_until(condition, seconds=WAIT_S):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if result := condition():
            return result
        time.sleep(0.02)
    return None


def devices_named(name):
    found = []
    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        if device.name == name:
            found.append(device)
        else:
            device.close()
    return found


class VirtualDevice:
    """A real input device, as far as the kernel and this app can tell."""

    def __init__(self, name=DEVICE_NAME, keys=None):
        codes = keys or [ecodes.KEY_F23, ecodes.KEY_F24, ecodes.KEY_LEFTCTRL]
        self.name = name
        self.ui = evdev.UInput({ecodes.EV_KEY: codes}, name=name)
        self.closed = False
        # udev gives the new device node its group a moment after it appears.
        assert wait_until(lambda: [d.close() or True for d in devices_named(name)])
        time.sleep(DEVICE_SETTLE_S)

    def key(self, code, value):
        self.ui.write(ecodes.EV_KEY, code, value)
        self.ui.syn()

    def close(self):
        if not self.closed:
            self.closed = True
            self.ui.close()


class TypedKeys:
    """Holds ydotool's virtual device exclusively and turns what it sends back into text."""

    def __enter__(self):
        self.device = next(
            d for d in map(evdev.InputDevice, evdev.list_devices()) if d.name.startswith("ydotoold")
        )
        self.device.grab()
        return self

    def __exit__(self, *_):
        self.device.ungrab()
        self.device.close()

    def names(self):
        """Names of the keys pressed since the last call, once the device has gone quiet."""
        pressed = []
        while select.select([self.device.fd], [], [], QUIET_READ_S)[0]:
            for event in self.device.read():
                if event.type == ecodes.EV_KEY and event.value == 1:
                    pressed.append(ecodes.KEY.get(event.code, str(event.code)))
        return pressed

    def text(self):
        """Replays Shift to give the text typed; a Shift tap on its own types nothing."""
        typed, shift = [], False
        while select.select([self.device.fd], [], [], QUIET_READ_S)[0]:
            for event in self.device.read():
                if event.type != ecodes.EV_KEY:
                    continue
                name = ecodes.KEY.get(event.code, str(event.code))
                if name == "KEY_LEFTSHIFT":
                    shift = event.value != 0
                elif event.value == 1:
                    typed.append(US_KEYS.get(name, (f"<{name}>",) * 2)[1 if shift else 0])
        return "".join(typed)


class SavedClipboard:
    """Puts back what the clipboard held; these checks must not cost you your copied text."""

    def __enter__(self):
        types = subprocess.run(
            ["wl-paste", "--list-types"], capture_output=True, text=True, check=False
        ).stdout
        self.kind = types.split()[0] if types.split() else None
        self.saved = (
            subprocess.run(
                ["wl-paste", "-n", "-t", self.kind], capture_output=True, check=False
            ).stdout
            if self.kind
            else b""
        )
        return self

    def read(self):
        return subprocess.run(
            ["wl-paste", "-n"], capture_output=True, text=True, check=False
        ).stdout

    def __exit__(self, *_):
        if self.kind:
            subprocess.run(["wl-copy", "-t", self.kind], input=self.saved, check=False)
        else:
            subprocess.run(["wl-copy", "--clear"], check=False)


@unittest.skipUnless(LIVE, "set CDA_LIVE_LINUX=1 on a Linux desktop")
class LiveInputTest(unittest.TestCase):
    def setUp(self):
        self.keyboard = VirtualDevice()
        self.addCleanup(self.keyboard.close)
        self.down, self.up, self.lost = threading.Event(), threading.Event(), threading.Event()

    def listener(self, shortcut="KEY_F24", device=DEVICE_NAME, grab=False, on_down=None):
        listener = hotkeys.TriggerListener(
            {"shortcut_key": shortcut, "shortcut_device": device, "grab": grab},
            on_down or self.down.set,
            self.up.set,
            self.lost.set,
        )
        listener.start()
        self.addCleanup(listener.stop)
        return listener

    def test_press_and_release_arrive_from_the_kernel(self):
        listener = self.listener()
        self.keyboard.key(ecodes.KEY_F24, 1)
        self.assertTrue(self.down.wait(WAIT_S))
        self.assertEqual(listener.held_keys(), {"KEY_F24"})
        self.keyboard.key(ecodes.KEY_F24, 0)
        self.assertTrue(self.up.wait(WAIT_S))
        self.assertEqual(listener.held_keys(), frozenset())

    def test_combination_fires_once_all_keys_are_down_and_ends_with_the_first_release(self):
        self.listener("KEY_LEFTCTRL+KEY_F24")
        self.keyboard.key(ecodes.KEY_F24, 1)
        time.sleep(0.1)
        self.assertFalse(self.down.is_set())
        self.keyboard.key(ecodes.KEY_LEFTCTRL, 1)
        self.assertTrue(self.down.wait(WAIT_S))
        self.keyboard.key(ecodes.KEY_LEFTCTRL, 0)
        self.assertTrue(self.up.wait(WAIT_S))
        self.keyboard.key(ecodes.KEY_F24, 0)

    def test_unplugging_mid_hold_reports_lost_and_no_release(self):
        listener = self.listener()
        self.keyboard.key(ecodes.KEY_F24, 1)
        self.assertTrue(self.down.wait(WAIT_S))
        self.keyboard.close()
        self.assertTrue(self.lost.wait(WAIT_S))
        self.assertFalse(self.up.is_set())
        self.assertTrue(wait_until(lambda: not listener.is_alive()))
        self.assertEqual(listener.held_keys(), frozenset())

    def test_key_held_across_a_reconnect_is_not_a_new_press(self):
        self.keyboard.key(ecodes.KEY_F24, 1)
        self.listener()
        self.keyboard.key(ecodes.KEY_F24, 2)  # auto-repeat, then the release
        self.keyboard.key(ecodes.KEY_F24, 0)
        time.sleep(0.2)
        self.assertFalse(self.down.is_set())
        self.assertFalse(self.up.is_set())

    def twin(self):
        """The listener's virtual twin, grabbed here so its keys stop at this test."""
        found = wait_until(lambda: devices_named(hotkeys.PASSTHROUGH_DEVICE_NAME))
        self.assertTrue(found, "no virtual twin appeared")
        device = found[0]
        self.addCleanup(device.close)
        device.grab()
        return device

    def replayed_presses(self, twin):
        presses = []
        while select.select([twin.fd], [], [], QUIET_READ_S)[0]:
            try:
                presses += [(e.code, e.value) for e in twin.read() if e.type == ecodes.EV_KEY]
            except OSError:  # the twin has closed; everything it sent was already read
                break
        return presses

    def test_grab_hides_the_shortcut_and_replays_every_other_key(self):
        self.listener(grab=True)
        twin = self.twin()
        self.keyboard.key(ecodes.KEY_F24, 1)
        self.assertTrue(self.down.wait(WAIT_S))
        self.keyboard.key(ecodes.KEY_F24, 0)
        self.keyboard.key(ecodes.KEY_F23, 1)
        self.keyboard.key(ecodes.KEY_F23, 0)
        self.assertEqual(self.replayed_presses(twin), [(ecodes.KEY_F23, 1), (ecodes.KEY_F23, 0)])

    def test_grabbed_combination_hides_its_key_and_keeps_the_modifier_working(self):
        self.listener("KEY_LEFTCTRL+KEY_F24", grab=True)
        twin = self.twin()
        self.keyboard.key(ecodes.KEY_F24, 1)  # alone: an ordinary key that reaches the app
        self.keyboard.key(ecodes.KEY_F24, 0)
        self.keyboard.key(ecodes.KEY_LEFTCTRL, 1)  # Ctrl + F23 still works as Ctrl + F23
        self.keyboard.key(ecodes.KEY_F23, 1)
        self.keyboard.key(ecodes.KEY_F23, 0)
        self.keyboard.key(ecodes.KEY_F24, 1)  # now the shortcut: F24 is hidden
        self.assertTrue(self.down.wait(WAIT_S))
        self.keyboard.key(ecodes.KEY_F24, 0)
        self.assertTrue(self.up.wait(WAIT_S))
        self.keyboard.key(ecodes.KEY_LEFTCTRL, 0)
        self.assertEqual(
            self.replayed_presses(twin),
            [
                (ecodes.KEY_F24, 1),
                (ecodes.KEY_F24, 0),
                (ecodes.KEY_LEFTCTRL, 1),
                (ecodes.KEY_F23, 1),
                (ecodes.KEY_F23, 0),
                (ecodes.KEY_LEFTCTRL, 0),
            ],
        )

    def test_grab_hides_a_mouse_side_button_so_it_cannot_mean_back(self):
        mouse = VirtualDevice(MOUSE_NAME, [ecodes.BTN_LEFT, ecodes.BTN_SIDE])
        self.addCleanup(mouse.close)
        self.listener("BTN_SIDE", MOUSE_NAME, grab=True)
        twin = self.twin()
        mouse.key(ecodes.BTN_SIDE, 1)
        self.assertTrue(self.down.wait(WAIT_S))
        mouse.key(ecodes.BTN_SIDE, 0)
        self.assertTrue(self.up.wait(WAIT_S))
        self.assertEqual(self.replayed_presses(twin), [])

    def test_stopping_removes_the_twin_so_no_replayed_key_stays_held(self):
        # The kernel releases a removed device's keys itself, and a reader then gets ENODEV
        # instead of what was still buffered, so the app's own release cannot be read here.
        listener = self.listener(grab=True)
        twin = self.twin()
        self.keyboard.key(ecodes.KEY_F23, 1)
        self.assertEqual(self.replayed_presses(twin), [(ecodes.KEY_F23, 1)])
        listener.stop()
        self.assertEqual(devices_named(hotkeys.PASSTHROUGH_DEVICE_NAME), [])
        self.keyboard.key(ecodes.KEY_F23, 0)

    def test_real_dropped_events_cancel_the_gesture_and_the_listener_lives_on(self):
        reading_blocked, can_read = threading.Event(), threading.Event()

        def block_the_reader():  # the callback runs on the listener's own thread
            reading_blocked.set()
            can_read.wait(WAIT_S)

        listener = self.listener(grab=True, on_down=block_the_reader)
        self.keyboard.key(ecodes.KEY_F24, 1)
        self.assertTrue(reading_blocked.wait(WAIT_S))
        for _ in range(2000):  # far more than the kernel keeps per device reader
            self.keyboard.key(ecodes.KEY_F24, 2)
        can_read.set()
        self.assertTrue(self.lost.wait(WAIT_S), "the kernel reported no dropped events")
        self.assertTrue(listener.is_alive())
        self.assertFalse(self.up.is_set())

    def test_capture_records_a_combination_with_its_device(self):
        found = []
        capture = threading.Thread(target=lambda: found.append(hotkeys.capture_next_input(WAIT_S)))
        capture.start()
        time.sleep(0.3)
        self.keyboard.key(ecodes.KEY_LEFTCTRL, 1)
        self.keyboard.key(ecodes.KEY_F24, 1)
        self.keyboard.key(ecodes.KEY_F24, 0)
        self.keyboard.key(ecodes.KEY_LEFTCTRL, 0)
        capture.join(WAIT_S + 1)
        self.assertEqual(found, [("KEY_LEFTCTRL+KEY_F24", DEVICE_NAME)])


@unittest.skipUnless(LIVE, "set CDA_LIVE_LINUX=1 on a Linux desktop")
class LiveTypingTest(unittest.TestCase):
    def test_ydotool_daemon_is_found(self):
        self.assertFalse(output.ydotool_socket_missing())

    def test_typed_text_arrives_as_exactly_those_keys(self):
        text = "Hello, it's “fine”…\tok"
        expected = 'Hello, it\'s "fine"... ok'
        with TypedKeys() as keys:
            result = output.deliver(text, dict(settings.DEFAULT_SETTINGS, copy_to_clipboard=False))
            self.assertEqual(keys.text(), expected)
        self.assertTrue(result.inserted, result.problem)

    def test_accents_are_refused_whole_instead_of_typed_with_holes(self):
        with TypedKeys() as keys:
            result = output.deliver(
                "café", dict(settings.DEFAULT_SETTINGS, copy_to_clipboard=False)
            )
            self.assertEqual(keys.names(), [])
        self.assertFalse(result.inserted)
        self.assertFalse(result.uncertain)
        self.assertIn("Paste", result.problem)

    @unittest.skipUnless(os.environ.get("WAYLAND_DISPLAY"), "Wayland session")
    def test_paste_puts_any_text_on_the_clipboard_and_presses_ctrl_v(self):
        with SavedClipboard() as clipboard, TypedKeys() as keys:
            result = output.deliver("café 日本語", dict(settings.DEFAULT_SETTINGS, output="paste"))
            self.assertEqual(keys.names(), ["KEY_LEFTCTRL", "KEY_V"])
            self.assertEqual(clipboard.read(), "café 日本語")
        self.assertTrue(result.inserted and result.copied, result.problem)

    @unittest.skipUnless(os.environ.get("WAYLAND_DISPLAY"), "Wayland session")
    def test_copy_off_leaves_the_clipboard_alone_even_on_failure(self):
        with SavedClipboard() as clipboard, TypedKeys():
            before = clipboard.read()
            output.deliver("café", dict(settings.DEFAULT_SETTINGS, copy_to_clipboard=False))
            self.assertEqual(clipboard.read(), before)

    @unittest.skipUnless(os.environ.get("WAYLAND_DISPLAY"), "Wayland session")
    def test_copy_on_still_copies_text_that_cannot_be_typed(self):
        with SavedClipboard() as clipboard, TypedKeys():
            result = output.deliver("café", dict(settings.DEFAULT_SETTINGS))
            self.assertEqual(clipboard.read(), "café")
        self.assertTrue(result.copied)
        self.assertFalse(result.inserted)


@unittest.skipUnless(LIVE, "set CDA_LIVE_LINUX=1 on a Linux desktop")
class LiveAudioTest(unittest.TestCase):
    def setUp(self):
        self.default = system_audio.default_output_device()
        if self.default is None:
            self.skipTest("no default output")
        self.was_muted = system_audio.device_muted(self.default)
        self.addCleanup(
            system_audio.set_device_muted, self.default, self.was_muted
        )  # whatever a failure leaves, the speakers go back

    def null_sink(self):
        """A temporary second output with no hardware; never the default."""
        name = f"cda_live_check_{os.getpid()}"
        module = subprocess.run(
            ["pactl", "load-module", "module-null-sink", f"sink_name={name}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.addCleanup(subprocess.run, ["pactl", "unload-module", module], check=False)

        def node_id():
            listing = subprocess.run(
                ["pw-cli", "ls", "Node"], capture_output=True, text=True, check=True
            ).stdout
            blocks = re.split(r"(?m)^\s*id (\d+), ", listing)
            for node, body in zip(blocks[1::2], blocks[2::2], strict=True):
                if f'node.name = "{name}"' in body:
                    return node
            return None

        return f"{system_audio.WPCTL}:{wait_until(node_id)}", module

    def test_mute_and_restore_on_the_real_default_output(self):
        if self.was_muted:
            self.skipTest("the speakers are muted already")
        system_audio.mute_output()
        self.assertTrue(system_audio.device_muted(self.default))
        system_audio.restore_output()
        self.assertFalse(system_audio.device_muted(self.default))
        system_audio.restore_output()  # safe to repeat
        self.assertFalse(system_audio.device_muted(self.default))

    def test_an_output_already_muted_stays_muted(self):
        system_audio.set_device_muted(self.default, True)
        system_audio.mute_output()
        system_audio.restore_output()
        self.assertTrue(system_audio.device_muted(self.default))

    def test_restore_reaches_the_muted_output_even_when_it_is_not_the_default(self):
        other, _ = self.null_sink()
        self.assertNotEqual(other, self.default)
        system_audio.set_device_muted(other, True)
        system_audio._record_path().write_text(json.dumps({"device": other}))
        system_audio.restore_output()
        self.assertFalse(system_audio.device_muted(other))
        self.assertFalse(system_audio._record_path().exists())
        self.assertEqual(system_audio.device_muted(self.default), self.was_muted)

    def test_an_output_that_has_gone_clears_the_record_without_an_error(self):
        other, module = self.null_sink()
        system_audio._record_path().write_text(json.dumps({"device": other}))
        subprocess.run(["pactl", "unload-module", module], check=True)
        time.sleep(0.2)
        system_audio.restore_output()
        self.assertFalse(system_audio._record_path().exists())


@unittest.skipUnless(LIVE, "set CDA_LIVE_LINUX=1 on a Linux desktop")
class LiveFeedbackTest(unittest.TestCase):
    def test_banner_shows_and_closes_through_the_desktop_service(self):
        banner = feedback.Banner()
        self.assertTrue(banner.available)
        banner.show("Claude Dictation Anywhere: live check")
        self.assertGreater(banner.notification_id, 0)
        banner.close()
        self.assertEqual(banner.notification_id, 0)

    def test_sounds_play_and_finish(self):
        started = time.monotonic()
        feedback.play_sound("start", wait=True)
        self.assertLess(time.monotonic() - started, feedback.SOUND_WAIT_TIMEOUT_S)


def tray_items():
    """What the desktop's tray lists, or None if this desktop has no such service."""
    result = subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.kde.StatusNotifierWatcher",
         "--object-path", "/StatusNotifierWatcher", "--method",
         "org.freedesktop.DBus.Properties.Get", "org.kde.StatusNotifierWatcher",
         "RegisteredStatusNotifierItems"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    return len(re.findall(r"'([^']+)'", result.stdout)) if result.returncode == 0 else None


@unittest.skipUnless(LIVE, "set CDA_LIVE_LINUX=1 on a Linux desktop")
class LiveAppTest(unittest.TestCase):
    """The whole program, as a separate process: settings page, tray, listener, typing, speakers."""

    @classmethod
    def setUpClass(cls):
        running = subprocess.run(
            ["pgrep", "-f", "claude[-_]dictation[-_]anywhere( |$)"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        if set(running) - {str(os.getpid())}:
            raise unittest.SkipTest("quit the running app first")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.options_file = cls.root / "options.json"
        cls.keylog = cls.root / "keys"
        cls.keylog.touch()
        cls.set_options(transcript="Hello from the live check.")
        program = cls.root / "claude"
        program.write_text(
            "#!/bin/sh\n"
            f'FAKE_CLAUDE_OPTIONS=$(cat "{cls.options_file}")\n'
            f'FAKE_CLAUDE_KEYLOG="{cls.keylog}"\n'
            "export FAKE_CLAUDE_OPTIONS FAKE_CLAUDE_KEYLOG\n"
            f'exec "{sys.executable}" -I "{FAKE_CLAUDE}" "$@"\n'
        )
        program.chmod(0o700)
        config = cls.root / "config" / settings.APP_NAME
        config.mkdir(parents=True)
        (config / "settings.toml").write_text(
            'shortcut_key = "KEY_F24"\n'
            f'shortcut_device = "{DEVICE_NAME}"\n'
            "grab = true\n"
            "copy_to_clipboard = false\n"
            f'claude_path = "{program}"\n'
        )
        (config / "settings.toml").chmod(0o644)  # as an earlier version left it
        cls.keyboard = VirtualDevice()
        cls.tray_before = tray_items()
        cls.log_path = cls.root / "app.log"
        with cls.log_path.open("wb") as log_file:
            cls.app = subprocess.Popen(
                [sys.executable, "-m", settings.__package__],
                env={
                    **os.environ,
                    "XDG_CONFIG_HOME": str(cls.root / "config"),
                    "XDG_DATA_HOME": str(cls.root / "data"),
                },
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        url_file = settings.runtime_dir() / "settings_url"
        assert wait_until(lambda: cls.app.poll() is not None or url_file.exists()), "no page"
        assert cls.app.poll() is None, cls.log_path.read_text()
        cls.url = url_file.read_text()
        with urllib.request.urlopen(cls.url) as reply:
            cls.token = re.search(r'TOKEN = "([^"]+)"', reply.read().decode()).group(1)
        cls.speakers = system_audio.default_output_device()
        assert wait_until(lambda: cls.status()["session_running"]), cls.log_path.read_text()
        assert wait_until(lambda: cls.status()["state"] == "idle")

    @classmethod
    def tearDownClass(cls):
        if cls.app.poll() is None:
            cls.app.kill()
            cls.app.wait()
        cls.keyboard.close()
        system_audio.restore_output()
        cls.tmp.cleanup()

    @classmethod
    def set_options(cls, **options):
        cls.options_file.write_text(json.dumps(options))

    @classmethod
    def api(cls, path, body=None):
        request = urllib.request.Request(
            cls.url.rstrip("/") + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-CDA-Token": cls.token},
        )
        with urllib.request.urlopen(request) as reply:
            return json.loads(reply.read())

    @classmethod
    def status(cls):
        return cls.api("/api/status")

    def state_is(self, state):
        self.assertTrue(
            wait_until(lambda: self.status()["state"] == state),
            f"still {self.status()['state']}: {self.log_tail()}",
        )

    def log_tail(self):
        return self.log_path.read_text()[-1500:]

    def press_f24(self):
        self.keyboard.key(ecodes.KEY_F24, 1)

    def release_f24(self):
        self.keyboard.key(ecodes.KEY_F24, 0)

    def muted(self):
        return bool(self.speakers) and system_audio.device_muted(self.speakers)

    def dictate(self, keys):
        """One held press; returns the text typed."""
        self.press_f24()
        self.state_is("listening")
        time.sleep(HOLD_S)
        self.release_f24()
        self.state_is("idle")
        return keys.text()

    def fresh_session(self, language):
        """Changing the language replaces the session, which reads the new options."""
        self.api("/api/settings", {"language": language})
        self.state_is("idle")

    def test_1_hold_types_mutes_and_remembers(self):
        self.assertEqual(self.status()["last_error"], "")
        warnings = [w["text"] for w in self.status()["warnings"]]
        self.assertEqual(warnings, [])
        if self.tray_before is not None:
            self.assertTrue(
                wait_until(lambda: tray_items() == self.tray_before + 1), "no tray item"
            )
        speakers_were_muted = self.muted()
        with TypedKeys() as keys:
            self.press_f24()
            self.state_is("listening")
            self.assertTrue(wait_until(self.muted), "the speakers were not muted")
            time.sleep(HOLD_S)
            self.release_f24()
            self.state_is("idle")
            self.assertEqual(keys.text(), "Hello from the live check.")
        self.assertEqual(self.muted(), speakers_were_muted)
        self.assertEqual(self.status()["history"][0]["text"], "Hello from the live check.")
        self.assertEqual(self.keylog.read_bytes().count(b"\r"), 0)  # Enter was never pressed

    def test_2_toggle_press_starts_and_second_press_stops(self):
        self.api("/api/settings", {"mode": "toggle"})
        self.addCleanup(self.api, "/api/settings", {"mode": "hold"})
        with TypedKeys() as keys:
            self.press_f24()
            self.release_f24()
            self.state_is("listening")
            time.sleep(HOLD_S)
            self.assertEqual(self.status()["state"], "listening")  # the release did not stop it
            self.press_f24()
            self.release_f24()
            self.state_is("idle")
            self.assertEqual(keys.text(), "Hello from the live check.")

    def test_3_press_while_transcribing_is_refused(self):
        self.set_options(transcript="Slow text.", processing_s=2)
        self.fresh_session("de")
        with TypedKeys() as keys:
            self.press_f24()
            self.state_is("listening")
            time.sleep(HOLD_S)
            self.release_f24()
            self.state_is("transcribing")
            self.press_f24()
            self.release_f24()
            self.state_is("idle")
            self.assertEqual(keys.text(), "Slow text.")
            time.sleep(0.5)
            self.assertEqual(self.status()["state"], "idle")  # the refused press stayed refused

    def test_4_text_a_us_keyboard_cannot_type_waits_on_the_page(self):
        self.set_options(transcript="café")
        self.fresh_session("fr")
        status = self.status()
        self.assertTrue(any("Choose Paste" in w["text"] for w in status["warnings"]))
        with TypedKeys() as keys:
            self.assertEqual(self.dictate(keys), "")
        entry = self.status()["recovery"][0]
        self.assertEqual(entry["text"], "café")
        self.assertIn("Paste", entry["reason"])
        self.api("/api/recovery/clear", {"id": entry["id"]})
        self.assertEqual(self.status()["recovery"], [])

    @unittest.skipUnless(os.environ.get("WAYLAND_DISPLAY"), "Wayland session")
    def test_5_paste_mode_delivers_those_characters(self):
        self.api("/api/settings", {"output": "paste"})
        self.addCleanup(self.api, "/api/settings", {"output": "type"})
        with SavedClipboard() as clipboard, TypedKeys() as keys:
            self.press_f24()
            self.state_is("listening")
            time.sleep(HOLD_S)
            self.release_f24()
            self.state_is("idle")
            self.assertEqual(keys.names(), ["KEY_LEFTCTRL", "KEY_V"])
            self.assertEqual(clipboard.read(), "café")

    def test_6_unplugging_mid_hold_keeps_the_text_and_a_new_plug_is_picked_up(self):
        speakers_were_muted = self.muted()
        with TypedKeys() as keys:
            self.press_f24()
            self.state_is("listening")
            time.sleep(HOLD_S)
            self.keyboard.close()
            self.state_is("idle")
            self.assertEqual(keys.names(), [])
        self.assertEqual(self.muted(), speakers_were_muted)
        self.assertEqual(self.status()["recovery"][0]["text"], "café")
        self.assertTrue(
            wait_until(
                lambda: any("Shortcut not active" in w["text"] for w in self.status()["warnings"]),
                15,
            )
        )
        type(self).keyboard = VirtualDevice()
        self.assertFalse(
            wait_until(
                lambda: (
                    not any("Shortcut not active" in w["text"] for w in self.status()["warnings"])
                ),
                15,
            )
            is None
        )
        self.set_options(transcript="Back again.")
        self.fresh_session("")
        with TypedKeys() as keys:
            self.assertEqual(self.dictate(keys), "Back again.")

    def test_7_expired_login_says_so_and_never_retries_while_held(self):
        connects = Path(f"{self.keylog}.connects")
        self.set_options(voice_refused="Voice stream error", logged_out=True)
        self.fresh_session("it")
        self.assertTrue(
            wait_until(lambda: "/login" in self.status()["last_error"]), self.log_tail()
        )
        self.assertFalse(self.status()["session_running"])  # said at start, with no key pressed
        self.press_f24()
        self.assertTrue(wait_until(lambda: connects.exists()), self.log_tail())
        time.sleep(1.5)  # the key stays held
        self.assertEqual(len(connects.read_text().splitlines()), 1)
        self.release_f24()
        self.state_is("idle")
        self.assertIn("/login", self.status()["last_error"])
        # After /login, the next press works with no restart of the app.
        self.set_options(transcript="Signed in.")
        with TypedKeys() as keys:
            self.assertEqual(self.dictate(keys), "Signed in.")
        self.assertEqual(self.status()["last_error"], "")

    def test_8_host_check_and_token_hold_on_the_running_server(self):
        request = urllib.request.Request(self.url, headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 403)
        self.assertNotIn(self.token, caught.exception.read().decode())
        request = urllib.request.Request(self.url.rstrip("/") + "/api/status")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 403)

    def test_9_quitting_leaves_nothing_behind(self):
        self.press_f24()  # quit in the middle of a recording, the hardest moment
        self.state_is("listening")
        time.sleep(HOLD_S)
        speakers_muted_while_recording = self.muted()
        self.app.send_signal(signal.SIGTERM)
        self.app.wait(QUIT_LIMIT_S)
        self.release_f24()
        self.assertEqual(self.app.returncode, 0, self.log_tail())
        leftovers = subprocess.run(
            ["pgrep", "-f", str(self.root)], capture_output=True, text=True, check=False
        ).stdout.split()
        self.assertEqual(leftovers, [])
        if speakers_muted_while_recording:
            self.assertFalse(self.muted())
        run = settings.runtime_dir()
        self.assertEqual(list(run.glob("session-*")), [])
        self.assertFalse((run / "settings_url").exists())
        self.assertFalse((run / system_audio.RECORD_FILE_NAME).exists())
        self.assertEqual(devices_named(hotkeys.PASSTHROUGH_DEVICE_NAME), [])
        if self.tray_before is not None:
            self.assertTrue(
                wait_until(lambda: tray_items() == self.tray_before), "tray item stayed"
            )
        config = self.root / "config" / settings.APP_NAME
        data = self.root / "data" / settings.APP_NAME
        for path in (config / "settings.toml", data / "history.jsonl"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), settings.PRIVATE_FILE_MODE, path)
        for path in (config, data, data / "logs", run):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), settings.PRIVATE_DIR_MODE, path)
        self.assertNotIn("Traceback", self.log_tail())


if LIVE and not shutil.which("wl-paste"):
    raise RuntimeError("the live checks need wl-clipboard")

if __name__ == "__main__":
    unittest.main()
