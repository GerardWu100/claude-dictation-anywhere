"""Put a transcript where you need it: the clipboard, and the app under your cursor.

Linux (Wayland): wl-copy for the clipboard, ydotool to type or press Ctrl+V.
Linux (X11):     xclip for the clipboard, ydotool to type or press Ctrl+V.
macOS:           pbcopy for the clipboard, pynput to type or press Cmd+V.
"""

import logging
import os
import shutil
import subprocess
import time

from .settings import IS_MAC

log = logging.getLogger(__name__)

# ydotool timing in "type" mode: ms between keys and ms each key is held.
# 2 ms keeps 100 characters under half a second; raise both if an app drops keys.
YDOTOOL_KEY_DELAY_MS = 2
YDOTOOL_KEY_HOLD_MS = 2
# ydotool uses Linux key codes: 29 = Left Ctrl, 47 = V; ":1" is down, ":0" up.
YDOTOOL_CTRL_V = ["29:1", "47:1", "47:0", "29:0"]
# Give the clipboard owner a moment before pasting, or some apps paste the old content.
PASTE_AFTER_COPY_S = 0.15

# Characters dictation likes to emit that a US keyboard cannot type, mapped to
# look-alikes (ydotool "type" drops anything not on the keyboard).
TYPEABLE_REPLACEMENTS = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "…": "...",
        " ": " ",
    }
)


def make_typeable(text: str) -> str:
    """Swap typographic characters for US-keyboard look-alikes before ydotool types them."""
    return text.translate(TYPEABLE_REPLACEMENTS)


def clipboard_command() -> list[str] | None:
    """The command that reads text on stdin into the clipboard, or None if none is installed."""
    if IS_MAC:
        return ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    return None


def missing_tools(settings: dict) -> list[str]:
    """Plain-language warnings for tools the current settings need but this system lacks."""
    if IS_MAC:
        return []
    warnings = []
    needs_clipboard = settings["copy_to_clipboard"] or settings["output"] == "paste"
    if needs_clipboard and clipboard_command() is None:
        warnings.append(
            "Copying to the clipboard needs wl-clipboard (Wayland) or xclip (X11). "
            "Arch: sudo pacman -S wl-clipboard   Debian/Ubuntu: sudo apt install wl-clipboard"
        )
    if settings["output"] != "none" and not shutil.which("ydotool"):
        warnings.append("Typing and pasting need ydotool with its ydotoold service running.")
    return warnings


def copy_to_clipboard(text: str) -> bool:
    """Copy with the system clipboard tool; False if none is installed."""
    command = clipboard_command()
    if command is None:
        return False
    subprocess.run(command, input=text.encode(), check=True)
    return True


def insert_text(text: str, method: str) -> None:
    """Type or paste ``text`` into the focused app. ``method``: "type", "paste", or "none"."""
    if method == "none":
        return
    if IS_MAC:
        _insert_mac(text, method)
        return
    if method == "paste":
        if not copy_to_clipboard(text):
            raise RuntimeError("Paste needs wl-clipboard or xclip installed.")
        time.sleep(PASTE_AFTER_COPY_S)
        subprocess.run(["ydotool", "key", *YDOTOOL_CTRL_V], check=True)
        return
    subprocess.run(
        ["ydotool", "type", "--key-delay", str(YDOTOOL_KEY_DELAY_MS),
         "--key-hold", str(YDOTOOL_KEY_HOLD_MS), "--escape", "0", "--file", "-"],
        input=make_typeable(text).encode(),
        check=True,
    )  # fmt: skip


def _insert_mac(text: str, method: str) -> None:
    from pynput.keyboard import Controller, Key

    keyboard = Controller()
    if method == "paste":
        copy_to_clipboard(text)
        time.sleep(PASTE_AFTER_COPY_S)
        with keyboard.pressed(Key.cmd):
            keyboard.tap("v")
    else:
        keyboard.type(text)


def deliver(text: str, settings: dict) -> None:
    """Copy (if enabled) and insert ``text`` as the settings say; logs, then re-raises, failures."""
    try:
        if settings["copy_to_clipboard"] and settings["output"] != "paste":
            copy_to_clipboard(text)
        insert_text(text, settings["output"])
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        log.warning("could not insert the text: %s", error)
        raise
