"""User settings: where they live, their defaults, and loading, checking, and saving them.

Settings are a flat dict saved as TOML. The settings page edits them; you can
also edit the file by hand while the app is stopped.

    Linux:  ~/.config/claude-dictation-anywhere/settings.toml
    macOS:  ~/Library/Application Support/claude-dictation-anywhere/settings.toml
"""

import os
import sys
import tempfile
import tomllib
from pathlib import Path

import tomli_w

APP_NAME = "claude-dictation-anywhere"
IS_MAC = sys.platform == "darwin"

# Languages Claude Code's dictation accepts (from its voice docs); "" = English.
DICTATION_LANGUAGES = (
    "", "cs", "da", "nl", "en", "fr", "de", "el", "hi", "id", "it",
    "ja", "ko", "no", "pl", "pt", "ru", "es", "sv", "tr", "uk",
)  # fmt: skip

# Every setting with its default. Comments say what each one does.
DEFAULT_SETTINGS = {
    # Key or button you press to dictate.
    # Linux: an input code such as "KEY_F8", "KEY_RIGHTCTRL", "BTN_SIDE" (mouse back).
    # macOS: a key name such as "Key.cmd_r", "Key.f8", or a mouse button "Button.button8".
    # The settings page fills this in when you press "Change".
    "shortcut_key": "Key.cmd_r" if IS_MAC else "KEY_RIGHTCTRL",
    # Linux only: exact name of the device that must send the key, so the same
    # key on another keyboard does nothing. "" = any device.
    "shortcut_device": "",
    # "hold": record while the key is held, stop on release.
    # "toggle": press once to start, press again to stop.
    "mode": "hold",
    # Linux only: stop the key's normal job while the app runs. Needed for mouse
    # side buttons (otherwise they also mean Back in the browser).
    "grab": False,
    # How the text reaches the app under your cursor.
    # "type": typed key by key (works in terminals; Linux: US-keyboard characters only).
    # "paste": copied, then Ctrl+V / Cmd+V (any characters; not in Linux terminals).
    # "none": not inserted; use the clipboard or the history page.
    "output": "type",
    # Also put every transcript on the clipboard.
    "copy_to_clipboard": True,
    # Dictation language code (see DICTATION_LANGUAGES); "" = English.
    "language": "",
    # Start the hidden Claude Code when the app starts, so the first press records at once.
    "prewarm": True,
    # Quit the hidden Claude Code after this many idle minutes to free about 250 MB.
    # 0 = keep it running.
    "idle_shutdown_minutes": 15,
    # Linux: show a "Listening / Transcribing" banner. The tray icon always changes colour.
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


def runtime_dir() -> Path:
    """Short-lived files: the lock, the editor hook, the transcript hand-off."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    path = Path(base) / f"{APP_NAME}-{os.getuid()}"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def load_settings(path: Path | None = None) -> dict:
    """Defaults overlaid with the saved file; entries that fail checks keep their default."""
    path = path or settings_path()
    settings = dict(DEFAULT_SETTINGS)
    if path.exists():
        saved = tomllib.loads(path.read_text(encoding="utf-8"))
        for name, value in saved.items():
            try:
                settings.update(check_settings({name: value}))
            except ValueError:
                pass
    return settings


def save_settings(settings: dict, path: Path | None = None) -> None:
    """Write atomically, so a crash never leaves half a file."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".toml.partial")
    partial.write_text(tomli_w.dumps(settings), encoding="utf-8")
    partial.replace(path)
