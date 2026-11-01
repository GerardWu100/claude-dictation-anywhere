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

import os
import threading
import time

import AVFoundation
import Quartz
from AppKit import NSWorkspace

TAP_START_TIMEOUT_S = 2
TAP_STOP_TIMEOUT_S = 2
# Pause after each typed character; some apps drop keys that arrive faster.
TYPE_KEY_DELAY_S = 0.002
# kVK_ANSI_V: the V position on a US layout, used for Cmd+V.
V_KEY_CODE = 9

INPUT_MONITORING_HELP = (
    "macOS blocks reading the shortcut. In System Settings → Privacy & Security → "
    "Input Monitoring, turn on the app that started this one (your terminal), then restart it."
)
MICROPHONE_HELP = (
    "macOS blocks the microphone. In System Settings → Privacy & Security → Microphone, "
    "turn on the app that started this one (your terminal), then restart it."
)
ACCESSIBILITY_HELP = (
    "macOS blocks typing. In System Settings → Privacy & Security → Accessibility, "
    "turn on the app that started this one (your terminal), then restart it."
)

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
MOUSE_BUTTON_NAMES = {
    2: "Mouse middle button",
    3: "Mouse back button",
    4: "Mouse forward button",
}
LEFT_RIGHT_BUTTONS = ("mouse:0", "mouse:1")

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
    return NAMED_KEYS.get(int(number), f"Key {number}")


def is_printable_key(key: str) -> bool:
    """True for keys that would also type into the focused app (letters, digits, Space...)."""
    kind, _, number = key.partition(":")
    return kind == "key" and number.isdigit() and int(number) not in NAMED_KEYS


def event_input(event_type: int, event) -> tuple[str, bool] | None:
    """(shortcut name, pressed) for a key or extra mouse button; None for anything else.

    Auto-repeat key downs and this process's own typing are dropped.
    """
    if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID) == os.getpid():
        return None
    if event_type in MOUSE_EVENTS:
        button = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventButtonNumber)
        return f"mouse:{button}", event_type == Quartz.kCGEventOtherMouseDown
    code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
    if event_type == Quartz.kCGEventFlagsChanged:
        bit = MODIFIER_FLAG_BITS.get(code)
        if bit is None:
            return None
        return f"key:{code}", bool(Quartz.CGEventGetFlags(event) & bit)
    if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
        return None
    return f"key:{code}", event_type == Quartz.kCGEventKeyDown


class EventTap:
    """A listen-only Quartz event tap on its own thread; calls ``handle(name, pressed)``.

    Listen-only: events still reach the focused app unchanged.

    Raises
    ------
    PermissionError
        From ``start()`` if macOS refuses Input Monitoring (asking once first).
    """

    def __init__(self, handle):
        self.handle = handle
        self._tap = None
        self._loop = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if not Quartz.CGPreflightListenEventAccess():
            Quartz.CGRequestListenEventAccess()  # adds the app to the list, off by default
            raise PermissionError(INPUT_MONITORING_HELP)
        self._thread = threading.Thread(target=self._run, daemon=True, name="event-tap")
        self._thread.start()
        self._ready.wait(TAP_START_TIMEOUT_S)
        if self._loop is None:
            raise PermissionError(INPUT_MONITORING_HELP)

    def stop(self) -> None:
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)
        if self._thread:
            self._thread.join(TAP_STOP_TIMEOUT_S)

    def _run(self) -> None:
        mask = 0
        for event_type in KEY_EVENTS + MOUSE_EVENTS:
            mask |= Quartz.CGEventMaskBit(event_type)
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._callback,
            None,
        )
        if self._tap is None:  # no permission after all
            self._ready.set()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        self._loop = loop
        self._ready.set()
        Quartz.CFRunLoopRun()  # until stop()
        Quartz.CFMachPortInvalidate(self._tap)

    def _callback(self, _proxy, event_type, event, _refcon):
        if event_type in TAP_DISABLED_EVENTS:
            # macOS switches a tap off when a callback is slow; switch it back on.
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        found = event_input(event_type, event)
        if found:
            self.handle(*found)
        return event


def can_listen() -> bool:
    return bool(Quartz.CGPreflightListenEventAccess())


def can_type() -> bool:
    return bool(Quartz.CGPreflightPostEventAccess())


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


def permission_warnings(needs_typing: bool) -> list[str]:
    """Plain-language fixes for permissions macOS has refused (the shortcut's is reported apart)."""
    warnings = []
    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
        AVFoundation.AVMediaTypeAudio
    )
    if status in (
        AVFoundation.AVAuthorizationStatusDenied,
        AVFoundation.AVAuthorizationStatusRestricted,
    ):
        warnings.append(MICROPHONE_HELP)
    if needs_typing and not can_type():
        warnings.append(ACCESSIBILITY_HELP)
    return warnings


def request_typing_permission() -> None:
    """Ask macOS to add this app under Accessibility (shows the system prompt once)."""
    Quartz.CGRequestPostEventAccess()


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


def type_text(text: str) -> None:
    """Type ``text`` one character at a time as Unicode, so any character and layout works."""
    for char in text:
        _post_key(0, 0, char)
        time.sleep(TYPE_KEY_DELAY_S)


def press_paste() -> None:
    """Press Cmd+V."""
    _post_key(V_KEY_CODE, Quartz.kCGEventFlagMaskCommand)
