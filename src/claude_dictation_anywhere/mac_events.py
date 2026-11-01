"""macOS input and permissions: read the shortcut, type the text, check privacy permissions.

Shortcut names:

    "key:<virtual key code>"   e.g. "key:54" Right Command, "key:100" F8
    "mouse:<button number>"    e.g. "mouse:3" back side button (0 left, 1 right, 2 middle)

Key codes are physical key positions (Carbon Events.h, kVK_*), so a shortcut
means the same key under any keyboard layout. Mouse buttons are read by number,
so side buttons work.

Permissions (System Settings → Privacy & Security), given to the app that
starts this program (Terminal, iTerm, ...):

    Input Monitoring   to see the shortcut
    Accessibility      to type or paste the text
    Microphone         for Claude Code's recording; until it is answered, Claude
                       Code freezes at "keep holding…", so the app asks at start
"""

import functools
import logging
import os
import re
import subprocess
import threading
import time

import ApplicationServices
import AVFoundation
import Quartz
from AppKit import NSWorkspace

log = logging.getLogger(__name__)

TAP_START_TIMEOUT_S = 2
TAP_STOP_TIMEOUT_S = 2
# The tap thread wakes this often to notice stop(). CFRunLoopStop is not enough:
# it does nothing to a run loop that has not started running yet.
TAP_RUN_SLICE_S = 0.25
# Pause after each typed character; some apps drop keys that arrive faster.
TYPE_KEY_DELAY_S = 0.002
# kVK_ANSI_V: the V position on a US layout, used for Cmd+V.
V_KEY_CODE = 9

# Privacy permission -> (what it is for, anchor of its System Settings page).
PERMISSIONS = {
    "Input Monitoring": ("reading the shortcut", "Privacy_ListenEvent"),
    "Accessibility": ("typing the text", "Privacy_Accessibility"),
    "Microphone": ("the microphone", "Privacy_Microphone"),
}
PRIVACY_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?"
FALLBACK_APP_NAME = "the app you started this from (your terminal)"

# Keys that type nothing, by key code, with the names the settings page shows.
# Any other key code types text and is refused as a shortcut.
NAMED_KEYS = {
    54: "Right Command", 55: "Left Command", 61: "Right Option", 58: "Left Option",
    62: "Right Control", 59: "Left Control", 60: "Right Shift", 56: "Left Shift",
    63: "Fn", 57: "Caps Lock", 53: "Escape", 114: "Help", 115: "Home", 119: "End",
    116: "Page Up", 121: "Page Down", 117: "Forward Delete",
    123: "Left Arrow", 124: "Right Arrow", 125: "Down Arrow", 126: "Up Arrow",
    122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6", 98: "F7", 100: "F8",
    101: "F9", 109: "F10", 103: "F11", 111: "F12", 105: "F13", 107: "F14", 113: "F15",
    106: "F16", 64: "F17", 79: "F18", 80: "F19", 90: "F20",
}  # fmt: skip
# Keys that type text, named by their place on a US keyboard (key codes are
# positions, so on other layouts the label may differ from the keycap).
TEXT_KEY_NAMES = {
    0: "A", 11: "B", 8: "C", 2: "D", 14: "E", 3: "F", 5: "G", 4: "H", 34: "I", 38: "J",
    40: "K", 37: "L", 46: "M", 45: "N", 31: "O", 35: "P", 12: "Q", 15: "R", 1: "S",
    17: "T", 32: "U", 9: "V", 13: "W", 7: "X", 16: "Y", 6: "Z",
    29: "0", 18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6", 26: "7", 28: "8", 25: "9",
    27: "-", 24: "=", 33: "[", 30: "]", 42: "\\", 41: ";", 39: "'", 43: ",", 47: ".",
    44: "/", 50: "`", 10: "§", 49: "Space", 36: "Return", 48: "Tab", 51: "Delete",
    65: "Keypad .", 67: "Keypad *", 69: "Keypad +", 71: "Clear", 75: "Keypad /",
    76: "Keypad Enter", 78: "Keypad -", 81: "Keypad =", 82: "Keypad 0", 83: "Keypad 1",
    84: "Keypad 2", 85: "Keypad 3", 86: "Keypad 4", 87: "Keypad 5", 88: "Keypad 6",
    89: "Keypad 7", 91: "Keypad 8", 92: "Keypad 9",
}  # fmt: skip
# Modifier key code -> its bit in the event flags. Modifiers send "flags changed"
# events, not key down/up; the bit says whether that one key is now down. The
# left/right bits are IOKit's NX_DEVICE*KEYMASK values.
MODIFIER_FLAG_BITS = {
    55: 0x08, 54: 0x10,  # Command left, right
    58: 0x20, 61: 0x40,  # Option left, right
    59: 0x01, 62: 0x2000,  # Control left, right
    56: 0x02, 60: 0x04,  # Shift left, right
    63: Quartz.kCGEventFlagMaskSecondaryFn,
    57: Quartz.kCGEventFlagMaskAlphaShift,  # Caps Lock: on while locked
}  # fmt: skip
# Its flag says "locked", not "held": it stays on after the key is let go, so it
# cannot be held to talk, and a press and release do not come as a pair.
CAPS_LOCK = "key:57"
MOUSE_BUTTON_NAMES = {
    2: "Mouse middle button",
    3: "Mouse back button",
    4: "Mouse forward button",
}
LEFT_RIGHT_BUTTONS = ("mouse:0", "mouse:1")
SHIFT_KEYS = frozenset({"key:56", "key:60"})

KEY_EVENTS = (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged)
MOUSE_EVENTS = (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp)
TAP_DISABLED_EVENTS = (
    Quartz.kCGEventTapDisabledByTimeout,
    Quartz.kCGEventTapDisabledByUserInput,
)


def describe_key(key: str) -> str:
    """Settings-page name of a shortcut, e.g. "key:54" -> "Right Command"."""
    kind, _, number = key.partition(":")
    if not number.isdigit():
        return key
    if kind == "mouse":
        return MOUSE_BUTTON_NAMES.get(int(number), f"Mouse button {int(number) + 1}")
    code = int(number)
    return NAMED_KEYS.get(code) or TEXT_KEY_NAMES.get(code) or f"Key {number}"


def is_printable_key(key: str) -> bool:
    """True for keys that would also type into the focused app (letters, digits, Space...)."""
    kind, _, number = key.partition(":")
    return kind == "key" and number.isdigit() and int(number) not in NAMED_KEYS


def is_known_key(key: str) -> bool:
    """True for "key:<number>" and "mouse:<number>"."""
    kind, _, number = key.partition(":")
    return kind in ("key", "mouse") and number.isdigit()


def is_modifier(key: str) -> bool:
    """Command, Option, Control, Shift, Fn, or Caps Lock."""
    kind, _, number = key.partition(":")
    return kind == "key" and number.isdigit() and int(number) in MODIFIER_FLAG_BITS


def event_input(event_type: int, event) -> tuple[str, bool, bool] | None:
    """(shortcut name, pressed, auto-repeat) for a key or extra mouse button; None otherwise.

    This process's own typing is dropped.
    """
    if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID) == os.getpid():
        return None
    if event_type in MOUSE_EVENTS:
        button = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventButtonNumber)
        return f"mouse:{button}", event_type == Quartz.kCGEventOtherMouseDown, False
    code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
    if event_type == Quartz.kCGEventFlagsChanged:
        bit = MODIFIER_FLAG_BITS.get(code)
        if bit is None:
            return None
        return f"key:{code}", bool(Quartz.CGEventGetFlags(event) & bit), False
    repeat = bool(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat))
    return f"key:{code}", event_type == Quartz.kCGEventKeyDown, repeat


@functools.cache
def launching_app_name() -> str:
    """Name of the app this program was started from, e.g. "Ghostty".

    macOS gives a program the privacy permissions of the app that started it,
    so the warnings name that app: the first ``*.app`` among the parent processes.
    """
    pid = os.getppid()
    while pid > 1:
        try:
            line = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()  # e.g. "1 /Applications/Ghostty.app/Contents/MacOS/ghostty"
        except (OSError, subprocess.CalledProcessError):
            break
        parent, _, command = line.partition(" ")
        app = re.search(r"([^/]+)\.app/Contents/MacOS/", command)
        if app:
            return app.group(1)
        pid = int(parent)
    return FALLBACK_APP_NAME


def permission_help(permission: str) -> str:
    """How to fix a missing permission, e.g. "macOS blocks typing the text: ... turn on Ghostty."""
    purpose, _ = PERMISSIONS[permission]
    return (
        f"macOS blocks {purpose}. In System Settings → Privacy & Security → {permission}, "
        f"turn on {launching_app_name()}. Restart this app if the message stays."
    )


def open_privacy_settings(permission: str) -> None:
    """Open the System Settings page where ``permission`` is turned on."""
    _, anchor = PERMISSIONS[permission]
    subprocess.Popen(["open", PRIVACY_SETTINGS_URL + anchor])


class PermissionMissing(PermissionError):
    """macOS refuses a privacy permission; ``permission`` is a key of ``PERMISSIONS``."""

    def __init__(self, permission: str):
        super().__init__(permission_help(permission))
        self.permission = permission


class TapStartTimeout(OSError):
    """The event tap did not start in time. Not a permission problem; a later retry may work."""


# macOS shows each permission prompt once per run; asking again from a retry loop is pointless.
_asked_permissions: set[str] = set()


def _ask_once(permission: str, request) -> None:
    if permission not in _asked_permissions:
        _asked_permissions.add(permission)
        request()


class EventTap:
    """A Quartz event tap on its own thread; calls ``handle(name, pressed, repeat)``.

    Events reach the focused app unchanged, except with ``can_hide`` when
    ``handle`` returns True: that event is then dropped. Hiding needs an active
    tap, which needs Accessibility; without it the tap only listens, and
    ``hides`` stays False. Listen-only taps cannot slow down typing, so use
    ``can_hide`` only when needed. ``on_interrupted`` runs if macOS switched the tap
    off, because events were missed.

    A start that fails or times out is cancelled for good: the thread cannot go
    on to create a tap afterwards, so at most one tap is ever active.

    Raises
    ------
    PermissionError
        From ``start()`` if macOS refuses Input Monitoring (asking once first).
    TapStartTimeout
        From ``start()`` if the thread did not get a tap ready in time.
    """

    def __init__(self, handle, can_hide: bool = False, on_interrupted=None):
        self.handle = handle
        self.can_hide = can_hide
        self.on_interrupted = on_interrupted
        self.hides = False
        self._tap = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()

    def start(self) -> None:
        if not Quartz.CGPreflightListenEventAccess():
            # Adds the app to the list, off by default.
            _ask_once("Input Monitoring", Quartz.CGRequestListenEventAccess)
            raise PermissionMissing("Input Monitoring")
        self._thread = threading.Thread(target=self._run, daemon=True, name="event-tap")
        self._thread.start()
        if not self._ready.wait(TAP_START_TIMEOUT_S):
            self.stop()
            raise TapStartTimeout("The shortcut listener did not start in time.")
        if self._tap is None:
            self.stop()
            raise PermissionMissing("Input Monitoring")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(TAP_STOP_TIMEOUT_S)

    def is_alive(self) -> bool:
        """False once the tap has stopped, by ``stop()`` or by failing."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        mask = 0
        for event_type in KEY_EVENTS + MOUSE_EVENTS:
            mask |= Quartz.CGEventMaskBit(event_type)
        hides = self.can_hide and can_type()
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault if hides else Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._callback,
            None,
        )
        if tap is None:  # no permission after all
            self._ready.set()
            return
        source = None
        loop = Quartz.CFRunLoopGetCurrent()
        try:
            if self._stop.is_set():  # the start timed out while the tap was being made
                return
            source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
            self.hides, self._tap = hides, tap
            self._ready.set()
            while not self._stop.is_set():
                Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, TAP_RUN_SLICE_S, False)
        finally:
            if source is not None:
                Quartz.CFRunLoopRemoveSource(loop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CFMachPortInvalidate(tap)

    def _callback(self, _proxy, event_type, event, _refcon):
        if event_type in TAP_DISABLED_EVENTS:
            # macOS switches a tap off when a callback is slow; switch it back on. Events
            # went missing meanwhile, perhaps the shortcut's release.
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, True)
            if self.on_interrupted is not None:
                self.on_interrupted()
            return event
        try:
            found = event_input(event_type, event)
            if found and self.handle(*found):
                return None  # hidden from every app
        except Exception:  # a failure here must never swallow the user's typing
            log.exception("shortcut handler failed")
        return event


def can_listen() -> bool:
    return bool(Quartz.CGPreflightListenEventAccess())


def can_type() -> bool:
    """True if Accessibility is on: either check, since they can disagree right after a grant."""
    return bool(ApplicationServices.AXIsProcessTrusted() or Quartz.CGPreflightPostEventAccess())


def request_microphone_if_unasked() -> None:
    """Show macOS's microphone prompt now if it was never answered, not at the first dictation.

    Claude Code inherits this app's answer (both belong to the app that started them).
    """
    audio = AVFoundation.AVMediaTypeAudio
    if AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(audio) == (
        AVFoundation.AVAuthorizationStatusNotDetermined
    ):
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            audio, lambda _granted: None
        )


def missing_permissions(needs_typing: bool) -> list[str]:
    """Microphone and Accessibility, if macOS refuses them (Input Monitoring is reported apart)."""
    missing = []
    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
        AVFoundation.AVMediaTypeAudio
    )
    if status in (
        AVFoundation.AVAuthorizationStatusDenied,
        AVFoundation.AVAuthorizationStatusRestricted,
    ):
        missing.append("Microphone")
    if needs_typing and not can_type():
        missing.append("Accessibility")
    return missing


def request_typing_permission() -> None:
    """Ask macOS to add this app under Accessibility (shows the system prompt once)."""
    _ask_once("Accessibility", Quartz.CGRequestPostEventAccess)


def frontmost_app_pid() -> int | None:
    """Process ID of the app that receives typing, or None if there is none."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.processIdentifier() if app else None


def _post_key(key_code: int, flags: int, text: str = "") -> None:
    """Post one key press and release. ``text`` replaces what the key would type."""
    for pressed in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, key_code, pressed)
        # Explicit flags: a shortcut modifier still held must not turn letters into shortcuts.
        Quartz.CGEventSetFlags(event, flags)
        if text:
            Quartz.CGEventKeyboardSetUnicodeString(event, len(text.encode("utf-16-le")) // 2, text)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def type_text(text: str, interrupted=None, check_every: int = 1) -> int:
    """Type ``text`` one character at a time as Unicode, so any character and layout works.

    Parameters
    ----------
    text : str
        What to type.
    interrupted : callable or None
        Asked before the first character and every ``check_every`` characters; True stops.
    check_every : int
        Characters between checks.

    Returns
    -------
    int
        Characters typed; fewer than ``len(text)`` if interrupted.
    """
    for index, char in enumerate(text):
        if interrupted is not None and index % check_every == 0 and interrupted():
            return index
        _post_key(0, 0, char)
        time.sleep(TYPE_KEY_DELAY_S)
    return len(text)


def press_paste() -> None:
    """Press Cmd+V."""
    _post_key(V_KEY_CODE, Quartz.kCGEventFlagMaskCommand)
