"""User settings: where they live, their defaults, and loading, checking, and saving them.

Settings are a flat dict saved as TOML. The settings page edits them; you can
also edit the file by hand while the app is stopped.

    Linux:  ~/.config/claude-dictation-anywhere/settings.toml
    macOS:  ~/Library/Application Support/claude-dictation-anywhere/settings.toml
"""

import logging
import os
import stat
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path

import tomli_w

APP_NAME = "claude-dictation-anywhere"
IS_MAC = sys.platform == "darwin"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
BROKEN_SETTINGS_INFIX = ".broken-"

log = logging.getLogger(__name__)

# Languages Claude Code's dictation accepts (from its voice docs); "" = English.
DICTATION_LANGUAGES = (
    "", "cs", "da", "nl", "en", "fr", "de", "el", "hi", "id", "it",
    "ja", "ko", "no", "pl", "pt", "ru", "es", "sv", "tr", "uk",
)  # fmt: skip

# Every setting with its default. Comments say what each one does.
DEFAULT_SETTINGS = {
    # Key or button you press to dictate.
    # Linux: an input code such as "KEY_F8", "KEY_RIGHTCTRL", "BTN_SIDE" (mouse back).
    # macOS: "key:<key code>" such as "key:54" (Right Command) or "key:100" (F8), or
    # "mouse:<button number>" such as "mouse:3" (back side button).
    # Keys pressed together are joined by "+", e.g. "key:59+key:58" (Control + Option).
    # The settings page fills this in when you press "Change".
    "shortcut_key": "key:54" if IS_MAC else "KEY_RIGHTCTRL",
    # "hold": record while the key is held, stop on release.
    # "toggle": press once to start, press again to stop.
    "mode": "hold",
    # Linux only: hide the shortcut's main key (U in Ctrl+U, F8, a mouse side
    # button) from other apps while the shortcut is held, as macOS always does, so
    # Ctrl+U does not also open a page's source and a side button does not also mean
    # Back. Off: the key also reaches the app in front. Modifier-only shortcuts
    # (Right Ctrl) are never hidden.
    "grab": True,
    # How the text reaches the app under your cursor.
    # "paste": arrives as one block: copied, then Shift+Insert (Linux) / Cmd+V (macOS).
    #   Any characters; works in browsers, editors, and terminals; uses the clipboard.
    # "type": typed key by key. When the focus is not in a text box, each letter can
    #   trigger that app's shortcuts. Linux: US-keyboard characters only.
    # "none": not inserted; use the clipboard or the history page.
    "output": "paste",
    # Also put every transcript on the clipboard.
    "copy_to_clipboard": True,
    # Dictation language code (see DICTATION_LANGUAGES); "" = English.
    "language": "",
    # Start the hidden Claude Code when the app starts, so the first press records at once.
    "prewarm": True,
    # Quit the hidden Claude Code after this many idle minutes to free about 250 MB.
    # The next press then waits 1-3 s for it to restart, and words spoken meanwhile
    # are lost. 0 = keep it running, so every press records at once.
    "idle_shutdown_minutes": 0,
    # Show a "Listening / Transcribing" banner (Linux: a notification; macOS: a pill at
    # the bottom of the screen). The tray icon always changes colour.
    "notifications": True,
    # Play a short sound when recording starts and stops.
    "sounds": True,
    # Mute the speakers while recording, so music or a podcast isn't dictated too.
    # Unmuted when you release; left muted if it already was.
    "mute_while_dictating": True,
    # Keep a history of transcripts on this computer (shown on the settings page).
    "history": True,
    # Path to the `claude` program. "" = find the Claude Code installed on this system.
    "claude_path": "",
}

ALLOWED_CHOICES = {
    "mode": ("hold", "toggle"),
    "output": ("type", "paste", "none"),
    "language": DICTATION_LANGUAGES,
}
MAX_IDLE_SHUTDOWN_MINUTES = 24 * 60


def config_dir() -> Path:
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def data_dir() -> Path:
    """History, logs, and the hidden session's working folder."""
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


class UnsafeFolderError(OSError):
    """The runtime folder is a symlink or belongs to someone else; it is never repaired."""


def make_private_dir(path: Path) -> Path:
    """Create ``path`` for this user only, and tighten it if this user owns a real folder there.

    Parent folders are created with the normal permissions and left alone. A
    symlink is followed and left alone too, so a config folder kept in a
    dotfiles repository still works.
    """
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) != PRIVATE_DIR_MODE
    ):
        path.chmod(PRIVATE_DIR_MODE)
    return path


def write_private_file(path: Path, text: str) -> None:
    """Replace ``path`` atomically with a file only this user can read.

    A unique temporary name in the same folder lets two writers run at once, and
    a crash leaves either the old file or the new one, never half a file.
    """
    handle, partial = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".partial")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(partial, path)
    except BaseException:
        Path(partial).unlink(missing_ok=True)
        raise


def tighten_private_file(path: Path) -> None:
    """Make an existing regular file of this user private; anything else is left alone."""
    try:
        info = path.lstat()
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
        ):
            path.chmod(PRIVATE_FILE_MODE)
    except OSError as error:
        log.warning("could not make %s private: %s", path.name, error.strerror)


def runtime_dir() -> Path:
    """Short-lived files: the lock, the mute record, each session's transcript hand-off.

    ``XDG_RUNTIME_DIR`` is private to the user and managed by the system. Without
    it, the folder lives in the user's own cache, never in a shared temporary
    folder: anyone can create a predictable name there first, and macOS deletes
    files in its temporary folder that have not been used for three days.

    Raises
    ------
    UnsafeFolderError
        If the path is a symlink or not a folder, or belongs to another user.
    OSError
        If the folder cannot be created.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        path = Path(base) / APP_NAME
    elif IS_MAC:
        path = Path.home() / "Library" / "Caches" / APP_NAME
    else:
        cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
        path = Path(cache) / APP_NAME / "run"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=PRIVATE_DIR_MODE)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeFolderError(f"{path} is a symlink or not a folder; move it away and restart.")
    if info.st_uid != os.getuid():
        raise UnsafeFolderError(f"{path} belongs to another user; move it away and restart.")
    if stat.S_IMODE(info.st_mode) != PRIVATE_DIR_MODE:
        path.chmod(PRIVATE_DIR_MODE)
    return path


def settings_path() -> Path:
    return config_dir() / "settings.toml"


def check_settings(update: dict) -> dict:
    """Return the known, valid entries of ``update``.

    Raises
    ------
    ValueError
        If update is not a dict, or a known setting has the wrong type or value.
    """
    if not isinstance(update, dict):
        raise ValueError("settings must be a JSON object")  # noqa: TRY004 - validation API
    clean = {}
    for name, value in update.items():
        if name not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[name]
        if type(value) is not type(default):
            raise ValueError(f"{name} must be {type(default).__name__}, got {value!r}")
        if name in ALLOWED_CHOICES and value not in ALLOWED_CHOICES[name]:
            raise ValueError(f"{name} must be one of {ALLOWED_CHOICES[name]}, got {value!r}")
        if name == "idle_shutdown_minutes" and not 0 <= value <= MAX_IDLE_SHUTDOWN_MINUTES:
            raise ValueError(f"idle_shutdown_minutes must be 0-{MAX_IDLE_SHUTDOWN_MINUTES}")
        clean[name] = value
    return clean


def _broken_settings_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}{BROKEN_SETTINGS_INFIX}*"))


def read_settings(path: Path | None = None) -> tuple[dict, list[str]]:
    """Defaults overlaid with the saved file, plus a plain-language list of what went wrong.

    A file that cannot be parsed or read is moved aside untouched, so a later
    save cannot overwrite it, and the app starts with defaults. The warning stays
    until the moved file is deleted or renamed back. A single invalid entry keeps
    its default; the valid entries around it still load.

    Returns
    -------
    (dict, list of str)
        Every setting in ``DEFAULT_SETTINGS``, and warnings for the settings page.
    """
    path = path or settings_path()
    settings = dict(DEFAULT_SETTINGS)
    problems = []
    try:
        saved = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        saved = {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        saved = {}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        kept = path.with_name(f"{path.name}{BROKEN_SETTINGS_INFIX}{stamp}")
        try:
            path.replace(kept)
        except OSError as move_error:
            problems.append(f"{path} could not be read or moved aside: {move_error.strerror}.")
        log.warning("could not read %s: %s", path.name, type(error).__name__)
    for name, value in saved.items():
        try:
            settings.update(check_settings({name: value}))
        except ValueError:
            problems.append(f"Setting {name} in {path.name} is invalid; using its default.")
    problems.extend(
        f"Your settings file could not be read, so the app is using defaults. It is kept at "
        f"{kept}. Fix it and rename it to {path.name}, or delete it to clear this message."
        for kept in _broken_settings_files(path)
    )
    return settings, problems


def load_settings(path: Path | None = None) -> dict:
    """``read_settings`` without its warnings, for the command-line test."""
    return read_settings(path)[0]


def save_settings(settings: dict, path: Path | None = None) -> None:
    """Write atomically and privately. Raises OSError; the caller keeps the old settings then."""
    path = path or settings_path()
    make_private_dir(path.parent)
    write_private_file(path, tomli_w.dumps(settings))
