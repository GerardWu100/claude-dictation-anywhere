"""Tell you what the app is doing without stealing focus: a banner and short sounds.

The tray icon changes colour on both platforms (see tray.py). The banner says
"Listening" / "Transcribing" where you are looking, and never takes keyboard
focus from the app the text is typed into:

    Linux:  a desktop notification, sent over D-Bus (GNOME Shell and others)
    macOS:  a small dark pill at the bottom of the screen under the mouse; it
            ignores clicks and shows on every Space, also over full-screen apps
"""

import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .settings import IS_MAC

if IS_MAC:
    import AppKit
    from PyObjCTools.AppHelper import callAfter

log = logging.getLogger(__name__)

# gdbus prefix for calls to the desktop notification service (GNOME Shell and others).
NOTIFICATIONS_DBUS = [
    "gdbus", "call", "--session",
    "--dest", "org.freedesktop.Notifications",
    "--object-path", "/org/freedesktop/Notifications",
]  # fmt: skip
# Urgency 2 (critical) keeps the banner up until closed; transient keeps it out of the history.
BANNER_HINTS = "{'urgency': <byte 2>, 'transient': <true>}"

# (start sound, stop sound) per platform; both ship with the operating system.
SOUNDS = (
    ("/System/Library/Sounds/Tink.aiff", "/System/Library/Sounds/Pop.aiff")
    if IS_MAC
    else (
        "/usr/share/sounds/freedesktop/stereo/device-added.oga",
        "/usr/share/sounds/freedesktop/stereo/device-removed.oga",
    )
)
SOUND_PLAYERS = ("afplay",) if IS_MAC else ("pw-play", "paplay")
SOUND_WAIT_TIMEOUT_S = 2
# A stuck notification service must not hold up the microphone or the speakers.
NOTIFY_TIMEOUT_S = 2

# macOS pill, in points.
PILL_MAX_WIDTH = 520
PILL_PADDING_X, PILL_PADDING_Y = 18, 9
PILL_MAX_CORNER_RADIUS = 18
PILL_GAP_ABOVE_DOCK = 28
PILL_FONT_SIZE = 14


class NotificationBanner:
    """One replaceable desktop notification (Linux)."""

    def __init__(self):
        self.available = bool(shutil.which("gdbus"))
        self.notification_id = 0  # 0 = none shown
        self._lock = threading.Lock()  # the worker and the "listening" announcer both call this

    def show(self, title: str) -> None:
        if not self.available:
            return
        with self._lock:
            try:
                result = subprocess.run(
                    [*NOTIFICATIONS_DBUS, "--method", "org.freedesktop.Notifications.Notify",
                     "Claude Dictation Anywhere", str(self.notification_id),
                     "audio-input-microphone", title, "", "[]", BANNER_HINTS, "0"],
                    capture_output=True, text=True, check=False, timeout=NOTIFY_TIMEOUT_S,
                )  # fmt: skip
            except (OSError, subprocess.SubprocessError) as error:
                log.warning("could not show the banner: %s", type(error).__name__)
                return
            match = re.search(r"uint32 (\d+)", result.stdout)
            self.notification_id = int(match.group(1)) if match else 0

    def close(self) -> None:
        with self._lock:
            if not self.notification_id:
                return
            try:
                subprocess.run(
                    [*NOTIFICATIONS_DBUS, "--method",
                     "org.freedesktop.Notifications.CloseNotification",
                     str(self.notification_id)],
                    capture_output=True, check=False, timeout=NOTIFY_TIMEOUT_S,
                )  # fmt: skip
            except (OSError, subprocess.SubprocessError) as error:
                log.warning("could not close the banner: %s", type(error).__name__)
            self.notification_id = 0


class PillBanner:
    """One replaceable floating pill (macOS). Safe to call from any thread.

    AppKit windows belong to the main thread, where the tray's event loop runs,
    so ``show`` and ``close`` queue their work there.
    """

    def __init__(self):
        self._panel = None
        self._background = None
        self._label = None

    def show(self, title: str) -> None:
        callAfter(self._show, title)

    def close(self) -> None:
        callAfter(self._close)

    def _create(self) -> None:
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (PILL_MAX_WIDTH, 40)),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(AppKit.NSStatusWindowLevel)  # above normal windows and the Dock
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorStationary
        )
        panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        background = AppKit.NSVisualEffectView.alloc().init()
        background.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
        background.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        background.setState_(AppKit.NSVisualEffectStateActive)
        background.setWantsLayer_(True)
        background.layer().setMasksToBounds_(True)
        label = AppKit.NSTextField.wrappingLabelWithString_("")
        label.setFont_(AppKit.NSFont.systemFontOfSize_weight_(PILL_FONT_SIZE, 0.23))  # medium
        label.setTextColor_(AppKit.NSColor.labelColor())
        label.setPreferredMaxLayoutWidth_(PILL_MAX_WIDTH - 2 * PILL_PADDING_X)
        background.addSubview_(label)
        panel.setContentView_(background)
        self._panel, self._background, self._label = panel, background, label

    def _show(self, title: str) -> None:
        if self._panel is None:
            self._create()
        self._label.setStringValue_(title)
        text = self._label.fittingSize()
        width = min(text.width, PILL_MAX_WIDTH - 2 * PILL_PADDING_X) + 2 * PILL_PADDING_X
        height = text.height + 2 * PILL_PADDING_Y
        self._label.setFrame_(
            ((PILL_PADDING_X, PILL_PADDING_Y), (width - 2 * PILL_PADDING_X, text.height))
        )
        self._background.layer().setCornerRadius_(min(height / 2, PILL_MAX_CORNER_RADIUS))
        visible = _screen_under_mouse().visibleFrame()  # excludes the menu bar and Dock
        x = visible.origin.x + (visible.size.width - width) / 2
        y = visible.origin.y + PILL_GAP_ABOVE_DOCK
        self._panel.setFrame_display_(((x, y), (width, height)), True)
        self._panel.orderFrontRegardless()  # shown without activating this app

    def _close(self) -> None:
        if self._panel is not None:
            self._panel.orderOut_(None)


def _screen_under_mouse():
    mouse = AppKit.NSEvent.mouseLocation()
    for screen in AppKit.NSScreen.screens():
        if AppKit.NSMouseInRect(mouse, screen.frame(), False):
            return screen
    return AppKit.NSScreen.mainScreen()


Banner = PillBanner if IS_MAC else NotificationBanner


def _finish_sound(sound: subprocess.Popen) -> None:
    """Wait for the sound to end; kill a player that has hung."""
    try:
        sound.wait(timeout=SOUND_WAIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log.warning("the sound player hung and was stopped")
        sound.kill()
        sound.wait()


def play_sound(which: str, wait: bool = False) -> None:
    """Play the "start" or "stop" sound; ``wait`` blocks until it ends (about 0.2 s).

    Never raises and never blocks longer than ``SOUND_WAIT_TIMEOUT_S``; a sound is
    not worth losing dictation over.
    """
    path = SOUNDS[0] if which == "start" else SOUNDS[1]
    player = next((p for p in SOUND_PLAYERS if shutil.which(p)), None)
    if not (player and Path(path).exists()):
        return
    try:
        sound = subprocess.Popen(
            [player, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as error:
        log.warning("could not play a sound: %s", type(error).__name__)
        return
    if wait:
        _finish_sound(sound)
    else:
        threading.Thread(target=_finish_sound, args=(sound,), daemon=True, name="sound").start()
