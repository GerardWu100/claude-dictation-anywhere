"""Linux app launcher: a .desktop file and icon so the app shows in GNOME/KDE app search.

``uv tool install`` only adds a terminal command; desktops list apps from
.desktop files. ``--install-launcher`` writes, per user (no root):

    ~/.local/share/applications/claude-dictation-anywhere.desktop
    ~/.local/share/icons/hicolor/256x256/apps/claude-dictation-anywhere.png

The launcher runs the app with ``--open``; if it already runs, that opens the
running copy's settings page instead.
"""

import os
import shutil
import sys
from pathlib import Path

from .settings import APP_NAME
from .tray import draw_icon

LAUNCHER_ICON_SIZE = 256


def share_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def desktop_file_path() -> Path:
    return share_dir() / "applications" / f"{APP_NAME}.desktop"


def icon_path() -> Path:
    size = f"{LAUNCHER_ICON_SIZE}x{LAUNCHER_ICON_SIZE}"
    return share_dir() / "icons" / "hicolor" / size / "apps" / f"{APP_NAME}.png"


def app_command() -> str:
    """Absolute path of this program, so the launcher works without PATH from a login shell."""
    found = shutil.which(APP_NAME)
    return found or str(Path(sys.argv[0]).absolute())


def desktop_entry(command: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Claude Dictation Anywhere\n"
        "Comment=Hold a key, speak, and Claude Code's dictation types it anywhere\n"
        f'Exec="{command}" --open\n'
        f"Icon={APP_NAME}\n"
        "Terminal=false\n"
        "Categories=Utility;Accessibility;\n"
        "Keywords=dictation;voice;speech;claude;transcribe;\n"
    )


def install_launcher() -> None:
    icon = icon_path()
    icon.parent.mkdir(parents=True, exist_ok=True)
    draw_icon("idle", LAUNCHER_ICON_SIZE).save(icon)
    entry = desktop_file_path()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(desktop_entry(app_command()), encoding="utf-8")
    print(f"Added the app launcher: {entry}")


def uninstall_launcher() -> None:
    for path in (desktop_file_path(), icon_path()):
        path.unlink(missing_ok=True)
    print("Removed the app launcher.")
