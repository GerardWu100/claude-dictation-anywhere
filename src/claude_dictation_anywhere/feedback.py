"""Tell you what the app is doing without stealing focus: a banner (Linux) and short sounds.

The tray icon is the main indicator on both platforms (see tray.py). On Linux a
GNOME notification also says "Listening" / "Transcribing"; it goes through the
desktop's notification service over D-Bus, so it never takes keyboard focus
from the app the text is typed into.
"""

import re
import shutil
import subprocess
from pathlib import Path

from .settings import IS_MAC

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


class Banner:
    """One replaceable notification banner (Linux; does nothing on macOS)."""

    def __init__(self):
        self.available = not IS_MAC and bool(shutil.which("gdbus"))
        self.notification_id = 0  # 0 = none shown

    def show(self, title: str) -> None:
        if not self.available:
            return
        result = subprocess.run(
            [*NOTIFICATIONS_DBUS, "--method", "org.freedesktop.Notifications.Notify",
             "Claude Dictation Anywhere", str(self.notification_id), "audio-input-microphone",
             title, "", "[]", BANNER_HINTS, "0"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        match = re.search(r"uint32 (\d+)", result.stdout)
        self.notification_id = int(match.group(1)) if match else 0

    def close(self) -> None:
        if not self.notification_id:
            return
        subprocess.run(
            [*NOTIFICATIONS_DBUS, "--method", "org.freedesktop.Notifications.CloseNotification",
             str(self.notification_id)],
            capture_output=True, check=False,
        )  # fmt: skip
        self.notification_id = 0


def play_sound(which: str) -> None:
    """Play the "start" or "stop" sound without waiting for it to finish."""
    path = SOUNDS[0] if which == "start" else SOUNDS[1]
    player = next((p for p in SOUND_PLAYERS if shutil.which(p)), None)
    if player and Path(path).exists():
        subprocess.Popen([player, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
