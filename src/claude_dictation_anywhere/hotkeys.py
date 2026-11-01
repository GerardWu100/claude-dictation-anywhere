"""Global shortcut: notice when the chosen key or mouse button goes down and up, in any app.

Linux reads the kernel's input devices directly (evdev), so it works on
Wayland and X11 alike; it needs your user in the `input` group. macOS uses a
Quartz event tap (see mac_events.py); it needs Input Monitoring permission for
the app that starts this program.

A shortcut is one key or button, or a combination joined by "+", such as
"key:59+key:58" (macOS Control + Option) or "KEY_LEFTCTRL+KEY_LEFTALT" (Linux).
A combination counts as pressed once all its keys are held, in any order, and
as released when any of them is let go.

Both platforms offer the same things:

    TriggerListener(settings, on_down, on_up).start() / .stop()
    capture_next_input(timeout_s) -> (shortcut, device) | None   # for "Change shortcut"
    describe_shortcut(shortcut, device) -> str                   # for the settings page
    shortcut_types_text(shortcut) -> bool                        # refused as a shortcut
"""

import logging
import os
import selectors
import threading
import time
from contextlib import ExitStack

from .settings import IS_MAC

log = logging.getLogger(__name__)

LISTENER_STOP_TIMEOUT_S = 2
PASSTHROUGH_DEVICE_NAME = "claude-dictation-anywhere passthrough"
# Virtual devices that replay synthetic events; listening to them would echo
# ydotool's own typing or our own passthrough back into the trigger.
IGNORED_DEVICE_PREFIXES = ("ydotoold", PASSTHROUGH_DEVICE_NAME)

COMBINATION_SEPARATOR = "+"

# Linux: friendly names for codes people pick most; others show the code without its prefix.
FRIENDLY_KEY_NAMES = {
    "BTN_SIDE": "Mouse back button",
    "BTN_EXTRA": "Mouse forward button",
    "BTN_MIDDLE": "Mouse middle button",
    "KEY_LEFTCTRL": "Left Ctrl",
    "KEY_RIGHTCTRL": "Right Ctrl",
    "KEY_LEFTALT": "Left Alt",
    "KEY_LEFTMETA": "Left Super",
    "KEY_LEFTSHIFT": "Left Shift",
    "KEY_RIGHTSHIFT": "Right Shift",
    "KEY_RIGHTALT": "Right Alt",
    "KEY_RIGHTMETA": "Right Super",
    "KEY_CAPSLOCK": "Caps Lock",
}


def shortcut_keys(shortcut: str) -> list[str]:
    """The keys of a shortcut, e.g. "key:59+key:58" -> ["key:59", "key:58"]."""
    return shortcut.split(COMBINATION_SEPARATOR)


class Combination:
    """Turns key events into one press and one release of a shortcut (one key or several).

    ``feed(key, pressed)`` takes every key event; keys outside the shortcut are
    ignored. ``on_down`` runs once all its keys are held, in any order;
    ``on_up`` runs as soon as any of them is let go.
    """

    def __init__(self, shortcut: str, on_down, on_up):
        self.keys = frozenset(shortcut_keys(shortcut))
        self.on_down, self.on_up = on_down, on_up
        self.held: set[str] = set()
        self._active = False

    def feed(self, key: str, pressed: bool) -> None:
        if key not in self.keys:
            return
        if pressed:
            self.held.add(key)
        else:
            self.held.discard(key)
        if pressed and not self._active and self.held == self.keys:
            self._active = True
            self.on_down()
        elif not pressed and self._active:
            self._active = False
            self.on_up()


def shortcut_types_text(shortcut: str) -> bool:
    """True if the shortcut would also type into the focused app: a text key plus at most Shift."""
    keys = shortcut_keys(shortcut)
    return any(is_printable_key(k) for k in keys) and all(
        is_printable_key(k) or k in SHIFT_KEYS for k in keys
    )


def describe_shortcut(shortcut: str, device: str) -> str:
    """Human-readable shortcut, e.g. "Control + Option" or "F8 on Keychron Link Keyboard"."""
    name = " + ".join(describe_key(k) for k in shortcut_keys(shortcut))
    return f"{name} on {' '.join(device.split())}" if device else name


if IS_MAC:
    from . import mac_events

    describe_key = mac_events.describe_key
    is_printable_key = mac_events.is_printable_key
    SHIFT_KEYS = mac_events.SHIFT_KEYS

    class TriggerListener:
        """Calls ``on_down`` / ``on_up`` when the shortcut is pressed / released (macOS).

        A key or button of the shortcut that is not a modifier (G in Command +
        Option + G, F8, a mouse side button) is hidden from other apps while the
        rest of the shortcut is held, so it does not also trigger something there.
        Shortcuts made only of modifiers are just watched.
        """

        def __init__(self, settings: dict, on_down, on_up):
            self.shortcut = settings["shortcut_key"]
            self.combination = Combination(self.shortcut, on_down, on_up)
            self._hidden: set[str] = set()  # pressed while hidden; their repeats and release too
            can_hide = not all(mac_events.is_modifier(k) for k in self.combination.keys)
            self._tap = mac_events.EventTap(self._handle, can_hide=can_hide)

        def _handle(self, name: str, pressed: bool, repeat: bool) -> bool:
            """Feed one event to the shortcut; True hides it from other apps."""
            if repeat:
                return name in self._hidden
            hide = False
            if name in self.combination.keys and not mac_events.is_modifier(name):
                rest_held = self.combination.keys - {name} <= self.combination.held
                if pressed and rest_held:
                    self._hidden.add(name)
                    hide = True
                elif not pressed and name in self._hidden:
                    self._hidden.discard(name)
                    hide = True
            self.combination.feed(name, pressed)
            return hide

        def start(self) -> None:
            self._tap.start()
            log.info("listening for %s", self.shortcut)

        def stop(self) -> None:
            self._tap.stop()

    def capture_next_input(timeout_s: float) -> tuple[str, str] | None:
        """Record the keys held together, until the first is let go; return (shortcut, "")."""
        found: list[str] = []  # in press order
        done = threading.Event()

        def take(name: str, pressed: bool, repeat: bool) -> None:
            # Left and right clicks are needed for clicking around; never a shortcut.
            if repeat or done.is_set() or name in mac_events.LEFT_RIGHT_BUTTONS:
                return
            if pressed and name not in found:
                found.append(name)
            elif not pressed and name in found:
                done.set()

        tap = mac_events.EventTap(take)
        tap.start()
        done.wait(timeout_s)
        tap.stop()
        return (COMBINATION_SEPARATOR.join(found), "") if found else None

else:
    import evdev
    from evdev import ecodes

    SHIFT_KEYS = frozenset({"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"})

    def describe_key(key: str) -> str:
        return FRIENDLY_KEY_NAMES.get(key) or key.removeprefix("KEY_").upper()

    def _real_devices() -> list["evdev.InputDevice"]:
        devices = []
        with ExitStack() as opened:
            for path in evdev.list_devices():
                device = evdev.InputDevice(path)
                if device.name.startswith(IGNORED_DEVICE_PREFIXES):
                    device.close()
                else:
                    opened.callback(device.close)
                    devices.append(device)
            opened.pop_all()
        return devices

    def _code_name(code: int) -> str:
        name = ecodes.BTN.get(code) or ecodes.KEY.get(code) or str(code)
        return name[0] if isinstance(name, list | tuple) else name  # aliases: BTN_LEFT/BTN_MOUSE

    def is_printable_key(key: str) -> bool:
        """Letters, digits, punctuation, and Space would also type into the focused app."""
        code = ecodes.ecodes.get(key)
        return code is not None and (
            ecodes.KEY_1 <= code <= ecodes.KEY_SLASH or code == ecodes.KEY_SPACE
        )

    class TriggerListener:
        """Calls ``on_down`` / ``on_up`` when the shortcut is pressed / released (Linux).

        With ``grab`` on, each matching device is taken exclusively and a virtual
        twin (uinput) replays every event except the shortcut's keys, so the
        pointer and other keys behave as before while those keys lose their
        normal job.
        """

        def __init__(self, settings: dict, on_down, on_up):
            self.key = settings["shortcut_key"]
            self.device_name = settings["shortcut_device"]
            self.grab = settings["grab"]
            self.on_down, self.on_up = on_down, on_up
            self._thread: threading.Thread | None = None
            self._pipe_lock = threading.Lock()
            self._wake_read, self._wake_write = os.pipe()  # writing here stops the loop

        def start(self) -> None:
            with ExitStack() as resources:
                resources.callback(self._close_pipes)
                # Key code -> shortcut key name, for every key of the shortcut.
                key_names = {}
                for name in shortcut_keys(self.key):
                    code = ecodes.ecodes.get(name)
                    if code is None:
                        raise ValueError(f"Unknown key code {name!r}")
                    key_names[code] = name
                devices = _real_devices()
                for device in devices:
                    resources.callback(device.close)
                devices = [
                    d
                    for d in devices
                    if key_names.keys() & set(d.capabilities().get(ecodes.EV_KEY, []))
                    and self.device_name in ("", d.name)
                ]
                if not devices:
                    raise ValueError(
                        f"No input device has {describe_shortcut(self.key, self.device_name)}"
                    )
                selector = resources.enter_context(selectors.DefaultSelector())
                selector.register(self._wake_read, selectors.EVENT_READ)
                passthrough = {}
                for device in devices:
                    if self.grab:
                        twin = evdev.UInput.from_device(device, name=PASSTHROUGH_DEVICE_NAME)
                        resources.callback(twin.close)
                        passthrough[device.fd] = twin
                        device.grab()
                    selector.register(device, selectors.EVENT_READ)
                    log.info("listening for %s on %s (%s)", self.key, device.path, device.name)
                # Device close releases the exclusive grab, including on setup failure.
                owned = resources.pop_all()
                self._thread = threading.Thread(
                    target=self._loop, args=(key_names, selector, passthrough, owned), daemon=True
                )
                try:
                    self._thread.start()
                except BaseException:
                    self._thread = None
                    owned.close()
                    raise

        def _close_pipes(self) -> None:
            """Release wake descriptors only after the worker has stopped using them."""
            with self._pipe_lock:
                for attribute in ("_wake_read", "_wake_write"):
                    fd = getattr(self, attribute)
                    if fd is not None:
                        os.close(fd)
                        setattr(self, attribute, None)

        def stop(self) -> None:
            with self._pipe_lock:
                if self._wake_write is not None:
                    os.write(self._wake_write, b"x")
            if self._thread:
                self._thread.join(timeout=LISTENER_STOP_TIMEOUT_S)
            else:
                self._close_pipes()

        def _loop(self, key_names, selector, passthrough, resources) -> None:
            combination = Combination(self.key, self.on_down, self.on_up)
            try:
                with resources:
                    while True:
                        for selected, _ in selector.select():
                            if selected.fileobj == self._wake_read:
                                return
                            device = selected.fileobj
                            for event in device.read():
                                if event.type == ecodes.EV_KEY and event.code in key_names:
                                    if event.value in (0, 1):  # 2 is auto-repeat
                                        combination.feed(key_names[event.code], event.value == 1)
                                    continue
                                if device.fd in passthrough:
                                    passthrough[device.fd].write_event(event)
            except OSError as error:
                log.warning("input device stopped: %s", error)

    def capture_next_input(timeout_s: float) -> tuple[str, str] | None:
        """Record the keys held together, until the first is let go; return (shortcut, device).

        The device name is set only when every key came from the same device.
        Left and right mouse buttons are skipped (you need them to click around),
        and so are touchpads' internal "finger" codes.
        """
        skip = {
            ecodes.BTN_LEFT,
            ecodes.BTN_RIGHT,
            ecodes.BTN_TOUCH,
            ecodes.BTN_TOOL_FINGER,
        }
        with ExitStack() as resources:
            devices = _real_devices()
            for device in devices:
                resources.callback(device.close)
            selector = resources.enter_context(selectors.DefaultSelector())
            for device in devices:
                if ecodes.EV_KEY in device.capabilities():
                    selector.register(device, selectors.EVENT_READ)
            found: list[str] = []  # in press order
            device_names: set[str] = set()

            def result() -> tuple[str, str]:
                device = next(iter(device_names)) if len(device_names) == 1 else ""
                return COMBINATION_SEPARATOR.join(found), device

            end = time.monotonic() + timeout_s
            while (remaining := end - time.monotonic()) > 0:
                for selected, _ in selector.select(timeout=remaining):
                    for event in selected.fileobj.read():
                        if event.type != ecodes.EV_KEY or event.code in skip:
                            continue
                        name = _code_name(event.code)
                        if event.value == 1 and name not in found:
                            found.append(name)
                            device_names.add(selected.fileobj.name)
                        elif event.value == 0 and name in found:
                            return result()
        return result() if found else None
