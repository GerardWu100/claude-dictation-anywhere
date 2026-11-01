"""Put a transcript where you need it: the clipboard, and the app under your cursor.

Linux (Wayland): wl-copy for the clipboard, ydotool to type or press Ctrl+V.
Linux (X11):     xclip for the clipboard, ydotool to type or press Ctrl+V.
macOS:           pbcopy for the clipboard, Quartz events to type or press Cmd+V
                 (see mac_events.py; needs Accessibility permission).

What the settings promise:

    copy_to_clipboard off   nothing is written to the clipboard, not even on a
                            failure; the text waits on the settings page instead.
                            Only "paste" mode uses the clipboard regardless, because
                            pasting is how it works; the settings page says so.
    target app              On macOS the app in front at release is compared with
                            the app in front just before inserting, and every
                            TYPE_FOCUS_CHECK_CHARS characters while typing. Only the
                            app is compared, so another window or tab of the same
                            app is not noticed. Linux cannot read the front window
                            on Wayland, so there is no check there.

``deliver`` reports what happened instead of raising, and nothing here ever
retries: after a failure part of the text may already be typed, and typing it
again would duplicate it.
"""

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from .settings import IS_MAC

if IS_MAC:
    from . import mac_events

log = logging.getLogger(__name__)

# ydotool timing in "type" mode: ms between keys and ms each key is held.
# 2 ms keeps 100 characters under half a second; raise both if an app drops keys.
YDOTOOL_KEY_DELAY_MS = 2
YDOTOOL_KEY_HOLD_MS = 2
# ydotool uses Linux key codes: 29 = Left Ctrl, 47 = V, 42 = Left Shift; ":1" is
# down, ":0" up.
YDOTOOL_CTRL_V = ["29:1", "47:1", "47:0", "29:0"]
# Some apps (seen in the Claude desktop app's chat box) drop the first ~40 ms of
# synthetic keys, ~10 characters, while their text field or input method wakes
# up. A Shift tap types nothing but wakes it; the pause lets it finish.
YDOTOOL_WAKE_KEY = ["42:1", "42:0"]
WAKE_BEFORE_TYPING_S = 0.25
# Give the clipboard owner a moment before pasting, or some apps paste the old content.
PASTE_AFTER_COPY_S = 0.15

# Deadlines for helper programs, so a hung one cannot stop the next dictation.
HELPER_TIMEOUT_S = 5
HELPER_POLL_S = 0.05
HELPER_KILL_WAIT_S = 1
# Typing time grows with the text: generous per character on top of the fixed deadline.
TYPE_TIMEOUT_PER_CHAR_S = 0.02
TYPE_FOCUS_CHECK_CHARS = 32

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
        "\u00a0": " ",
    }
)


class HelperCancelled(RuntimeError):
    """The app is pausing or quitting; the helper program was killed."""


class HelperTimeout(RuntimeError):
    """The helper program missed its deadline and was killed."""


class InsertionFailed(RuntimeError):
    """The text was not fully inserted. ``uncertain``: part of it may be in the app."""

    def __init__(self, message: str, uncertain: bool):
        super().__init__(message)
        self.uncertain = uncertain


@dataclass
class Delivery:
    """What ``deliver`` did. ``copied`` and ``inserted`` are separate successes."""

    copied: bool = False
    inserted: bool = False
    uncertain: bool = False  # part of the text may be in the app; never retype it
    problem: str = ""  # plain-language, for the user; "" if everything asked for worked


def run_helper(args: list[str], stdin: bytes | None, timeout_s: float, cancel=None) -> None:
    """Run a helper program to completion, killing it on deadline or cancellation.

    Parameters
    ----------
    args : list of str
        The command; text goes through ``stdin``, never here (arguments are visible
        to other programs).
    stdin : bytes or None
        Written to the program's standard input.
    timeout_s : float
        Deadline in seconds.
    cancel : threading.Event or None
        Set to stop early.

    Raises
    ------
    OSError
        The program could not start.
    subprocess.CalledProcessError
        It exited with an error.
    HelperTimeout, HelperCancelled
        It was killed.
    """
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout_s
    pending = stdin
    while True:
        try:
            process.communicate(pending, timeout=HELPER_POLL_S)
            break
        except subprocess.TimeoutExpired:
            pending = None  # communicate() keeps feeding what it was first given
        cancelled = cancel is not None and cancel.is_set()
        if cancelled or time.monotonic() > deadline:
            process.kill()
            try:
                process.wait(timeout=HELPER_KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                log.warning("%s did not exit after being killed", args[0])
            raise HelperCancelled(args[0]) if cancelled else HelperTimeout(args[0])
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, args[0])


def make_typeable(text: str) -> str:
    """Swap typographic characters for US-keyboard look-alikes before ydotool types them."""
    return text.translate(TYPEABLE_REPLACEMENTS)


def without_line_breaks(text: str) -> str:
    """A typed line break is an Enter key, which sends the message in chats and terminals."""
    return " ".join(text.splitlines())


def clipboard_command() -> list[str] | None:
    """The command that reads text on stdin into the clipboard, or None if none is installed."""
    if IS_MAC:
        return ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    return None


def setup_warnings(settings: dict) -> list[dict]:
    """Fixes for tools or permissions the current settings need but lack.

    Returns
    -------
    list of dict
        ``{"text": plain-language fix, "permission": macOS privacy permission or None}``;
        the settings page offers to open that permission's System Settings page.
    """
    if IS_MAC:
        return [
            {"text": mac_events.permission_help(p), "permission": p}
            for p in mac_events.missing_permissions(needs_typing=settings["output"] != "none")
        ]
    warnings = []
    needs_clipboard = settings["copy_to_clipboard"] or settings["output"] == "paste"
    if needs_clipboard and clipboard_command() is None:
        warnings.append(
            "Copying to the clipboard needs wl-clipboard (Wayland) or xclip (X11). "
            "Arch: sudo pacman -S wl-clipboard   Debian/Ubuntu: sudo apt install wl-clipboard"
        )
    if settings["output"] != "none" and not shutil.which("ydotool"):
        warnings.append("Typing and pasting need ydotool with its ydotoold service running.")
    return [{"text": warning, "permission": None} for warning in warnings]


def copy_to_clipboard(text: str, cancel=None) -> bool:
    """Copy with the system clipboard tool; False if none is installed."""
    command = clipboard_command()
    if command is None:
        return False
    run_helper(command, text.encode(), HELPER_TIMEOUT_S, cancel)
    return True


def insert_text(text: str, method: str, target_app: int | None = None, cancel=None) -> None:
    """Type or paste ``text`` into the focused app. ``method``: "type", "paste", or "none".

    Raises
    ------
    InsertionFailed
        With ``uncertain`` set if part of the text may have reached the app.
    """
    if method == "none":
        return
    if IS_MAC:
        _insert_mac(text, method, target_app, cancel)
    elif method == "paste":
        _paste_linux(text, cancel)
    else:
        _type_linux(text, cancel)


def _paste_linux(text: str, cancel) -> None:
    try:
        copied = copy_to_clipboard(text, cancel)
    except (OSError, subprocess.SubprocessError, HelperTimeout, HelperCancelled) as error:
        raise InsertionFailed("could not copy it for pasting", uncertain=False) from error
    if not copied:
        raise InsertionFailed("paste needs wl-clipboard or xclip installed", uncertain=False)
    time.sleep(PASTE_AFTER_COPY_S)
    try:
        run_helper(["ydotool", "key", *YDOTOOL_CTRL_V], None, HELPER_TIMEOUT_S, cancel)
    except OSError as error:
        raise InsertionFailed("ydotool could not start", uncertain=False) from error
    except (subprocess.SubprocessError, HelperTimeout, HelperCancelled) as error:
        raise InsertionFailed("pasting did not finish", uncertain=True) from error


def _type_linux(text: str, cancel) -> None:
    try:
        run_helper(["ydotool", "key", *YDOTOOL_WAKE_KEY], None, HELPER_TIMEOUT_S, cancel)
    except (OSError, subprocess.SubprocessError, HelperTimeout, HelperCancelled) as error:
        raise InsertionFailed("ydotool could not start typing", uncertain=False) from error
    time.sleep(WAKE_BEFORE_TYPING_S)
    typed = make_typeable(without_line_breaks(text))
    command = ["ydotool", "type", "--key-delay", str(YDOTOOL_KEY_DELAY_MS),
               "--key-hold", str(YDOTOOL_KEY_HOLD_MS), "--escape", "0", "--file", "-"]  # fmt: skip
    try:
        run_helper(
            command,
            typed.encode(),
            HELPER_TIMEOUT_S + len(typed) * TYPE_TIMEOUT_PER_CHAR_S,
            cancel,
        )
    except OSError as error:
        raise InsertionFailed("ydotool could not start", uncertain=False) from error
    except (subprocess.SubprocessError, HelperTimeout, HelperCancelled) as error:
        raise InsertionFailed("typing did not finish", uncertain=True) from error


def _insert_mac(text: str, method: str, target_app: int | None, cancel) -> None:
    if not mac_events.can_type():
        mac_events.request_typing_permission()
        raise InsertionFailed(mac_events.permission_help("Accessibility"), uncertain=False)
    if method == "paste":
        try:
            copy_to_clipboard(text, cancel)
        except (OSError, subprocess.SubprocessError, HelperTimeout, HelperCancelled) as error:
            raise InsertionFailed("could not copy it for pasting", uncertain=False) from error
        time.sleep(PASTE_AFTER_COPY_S)
        mac_events.press_paste()
        return

    def interrupted() -> bool:
        cancelled = cancel is not None and cancel.is_set()
        return cancelled or (target_app is not None and focused_app() != target_app)

    typed = mac_events.type_text(without_line_breaks(text), interrupted, TYPE_FOCUS_CHECK_CHARS)
    if typed < len(without_line_breaks(text)):
        raise InsertionFailed(f"typing stopped after {typed} characters", uncertain=typed > 0)


def focused_app() -> int | None:
    """macOS: process ID of the app in front, to check later that it is still there; else None."""
    return mac_events.frontmost_app_pid() if IS_MAC else None


def deliver(text: str, settings: dict, target_app: int | None = None, cancel=None) -> Delivery:
    """Copy (if enabled) and insert ``text`` as the settings say, and report the outcome.

    Parameters
    ----------
    text : str
        The transcript.
    settings : dict
        Uses "output" and "copy_to_clipboard".
    target_app : int or None
        ``focused_app()`` at release. If another app is in front now, the text is
        not inserted, so it never lands in the wrong app.
    cancel : threading.Event or None
        Set when the app pauses or quits, to stop a slow copy or typing.
    """
    method = settings["output"]
    result = Delivery()
    if settings["copy_to_clipboard"] and method != "paste":
        try:
            result.copied = copy_to_clipboard(text, cancel)
            if not result.copied:
                result.problem = "No clipboard tool is installed, so it was not copied."
        except (OSError, subprocess.SubprocessError, HelperTimeout, HelperCancelled) as error:
            log.warning("could not copy the text: %s", type(error).__name__)
            result.problem = "Could not copy it to the clipboard."
    if method == "none":
        return result
    if cancel is not None and cancel.is_set():
        result.problem = "Stopped before inserting it."
        return result
    if target_app is not None and focused_app() != target_app:
        result.problem = "You switched apps before it finished, so it was not typed."
        return result
    try:
        insert_text(text, method, target_app, cancel)
        result.inserted = True
        if method == "paste":
            result.copied = True
    except InsertionFailed as error:
        log.warning("could not insert the text: %s", type(error).__name__)
        result.uncertain = error.uncertain
        result.problem = f"Could not insert it: {error}."
        if error.uncertain:
            result.problem += " Part of it may be in the app already."
    return result
