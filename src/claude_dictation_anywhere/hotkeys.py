"""Global shortcut: notice when the chosen key or mouse button goes down and up, in any app.

Linux reads the kernel's input devices directly (evdev), so it works on
Wayland and X11 alike; it needs your user in the `input` group. macOS uses
pynput (Quartz event taps); it needs Accessibility and Input Monitoring
permission for the app that starts this program.

Both platforms offer the same two things:

    TriggerListener(settings, on_down, on_up).start() / .stop()
    capture_next_input(timeout_s) -> (key, device) | None   # for "Change shortcut"
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

# Friendly names for codes people pick most; others are shown as the code without its prefix.
FRIENDLY_KEY_NAMES = {
    "BTN_SIDE": "Mouse back button",
    "BTN_EXTRA": "Mouse forward button",
    "BTN_MIDDLE": "Mouse middle button",
    "KEY_RIGHTCTRL": "Right Ctrl",
    "KEY_RIGHTALT": "Right Alt",
    "KEY_RIGHTMETA": "Right Super",
    "KEY_CAPSLOCK": "Caps Lock",
    "Key.cmd_r": "Right Command",
    "Key.alt_r": "Right Option",
    "Key.ctrl_r": "Right Control",
}


def describe_shortcut(key: str, device: str) -> str:
    """Human-readable shortcut, e.g. "F8 on Keychron Link Keyboard"."""
    name = FRIENDLY_KEY_NAMES.get(key) or key.removeprefix("KEY_").removeprefix("Key.").upper()
    return f"{name} on {' '.join(device.split())}" if device else name


if IS_MAC:
    from pynput import keyboard, mouse

    def _pynput_name(item) -> str:
        """Stable name for a pynput key or button: "Key.f8", "Button.button8", or "char:a"."""
        char = getattr(item, "char", None)
        return f"char:{char}" if char else str(item)

    class TriggerListener:
        """Calls ``on_down`` / ``on_up`` when the shortcut is pressed / released (macOS)."""

        def __init__(self, settings: dict, on_down, on_up):
            self.key = settings["shortcut_key"]
            self.on_down, self.on_up = on_down, on_up
            self._held = False
            self._listeners = []

        def _handle(self, item, pressed: bool) -> None:
            if _pynput_name(item) != self.key:
                return
            if pressed and not self._held:  # ignore auto-repeat
                self._held = True
                self.on_down()
            elif not pressed and self._held:
                self._held = False
                self.on_up()

        def start(self) -> None:
            self._listeners = [
                keyboard.Listener(
                    on_press=lambda k: self._handle(k, True),
                    on_release=lambda k: self._handle(k, False),
                ),
                mouse.Listener(on_click=lambda _x, _y, b, pressed: self._handle(b, pressed)),
            ]
            for listener in self._listeners:
                listener.start()
            log.info("listening for %s", self.key)

        def stop(self) -> None:
            for listener in self._listeners:
                listener.stop()

    def capture_next_input(timeout_s: float) -> tuple[str, str] | None:
        """Wait for the next key or mouse-button press anywhere; return (name, "")."""
        found: list[str] = []
        done = threading.Event()

        def take(item):
            name = _pynput_name(item)
            if name in ("Button.left", "Button.right"):
                return  # needed for clicking around; never a shortcut
            found.append(name)
            done.set()

        listeners = [
            keyboard.Listener(on_press=take),
            mouse.Listener(on_click=lambda _x, _y, b, pressed: pressed and take(b)),
        ]
        for listener in listeners:
            listener.start()
        done.wait(timeout_s)
        for listener in listeners:
            listener.stop()
        return (found[0], "") if found else None

    def is_printable_key(key: str) -> bool:
        return key.startswith("char:") or key == "Key.space"

else:
    import evdev
    from evdev import ecodes

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
        twin (uinput) replays every event except the trigger, so the pointer and
        other keys behave as before while the trigger loses its normal job.
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
                key_code = ecodes.ecodes.get(self.key)
                if key_code is None:
                    raise ValueError(f"Unknown key code {self.key!r}")
                devices = _real_devices()
                for device in devices:
                    resources.callback(device.close)
                devices = [
                    d
                    for d in devices
                    if key_code in d.capabilities().get(ecodes.EV_KEY, [])
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
                    target=self._loop, args=(key_code, selector, passthrough, owned), daemon=True
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

        def _loop(self, key_code, selector, passthrough, resources) -> None:
            try:
                with resources:
                    while True:
                        for selected, _ in selector.select():
                            if selected.fileobj == self._wake_read:
                                return
                            device = selected.fileobj
                            for event in device.read():
                                if event.type == ecodes.EV_KEY and event.code == key_code:
                                    if event.value == 1:
                                        self.on_down()
                                    elif event.value == 0:
                                        self.on_up()
                                    continue
                                if device.fd in passthrough:
                                    passthrough[device.fd].write_event(event)
            except OSError as error:
                log.warning("input device stopped: %s", error)

    def capture_next_input(timeout_s: float) -> tuple[str, str] | None:
        """Wait for the next key or button press on any real device; return (code, device name).

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
            end = time.monotonic() + timeout_s
            while (remaining := end - time.monotonic()) > 0:
                for selected, _ in selector.select(timeout=remaining):
                    for event in selected.fileobj.read():
                        if (
                            event.type == ecodes.EV_KEY
                            and event.value == 1
                            and event.code not in skip
                        ):
                            return _code_name(event.code), selected.fileobj.name
        return None
