"""App launchers, so the app starts from the desktop's app search like any other app.

``uv tool install`` only adds a terminal command. ``--install-launcher`` adds, per
user (no root):

    Linux:  ~/.local/share/applications/claude-dictation-anywhere.desktop
            ~/.local/share/icons/hicolor/256x256/apps/claude-dictation-anywhere.png
    macOS:  ~/Applications/Claude Dictation Anywhere.app (found by Spotlight)

Both run the app with ``--open``. On Linux, if it already runs, that opens the running
copy's settings page instead.

The macOS app is an AppleScript applet from ``osacompile``, which ships with macOS,
because macOS gives privacy permissions to the app that started a program. A compiled
applet stays open while the program runs, so the permissions belong to this app, not
to a terminal; a plain script as the bundle's program does not reliably get that.
The applet only runs the installed command, so `uv tool upgrade` does not change it
and the permissions survive upgrades.
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .icon import draw_icon
from .settings import APP_NAME, IS_MAC

LAUNCHER_ICON_SIZE = 256
MAC_ICON_SIZE = 1024
DISPLAY_NAME = "Claude Dictation Anywhere"
MAC_BUNDLE_ID = "io.github.gerardwu100.claude-dictation-anywhere"
MAC_MICROPHONE_REASON = "Claude Code records your voice for dictation while you hold the shortcut."
# A program started from the Finder gets a minimal PATH and no language. Claude Code may
# need PATH to find Node, and pbcopy garbles accents without a UTF-8 language.
MAC_ENVIRONMENT = (
    'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; '
    'export LANG="${LANG:-en_US.UTF-8}"; '
)
MAC_FAILED_MESSAGE = (
    f"{DISPLAY_NAME} stopped or is already running. Run {APP_NAME} in a terminal to see why."
)


def share_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def desktop_file_path() -> Path:
    return share_dir() / "applications" / f"{APP_NAME}.desktop"


def icon_path() -> Path:
    size = f"{LAUNCHER_ICON_SIZE}x{LAUNCHER_ICON_SIZE}"
    return share_dir() / "icons" / "hicolor" / size / "apps" / f"{APP_NAME}.png"


def mac_app_path() -> Path:
    return Path.home() / "Applications" / f"{DISPLAY_NAME}.app"


def app_command() -> str:
    """Absolute path of this program, so the launcher works without PATH from a login shell."""
    found = shutil.which(APP_NAME)
    return found or str(Path(sys.argv[0]).absolute())


def desktop_entry(command: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={DISPLAY_NAME}\n"
        "Comment=Hold a key, speak, and Claude Code's dictation types it anywhere\n"
        f'Exec="{command}" --open\n'
        f"Icon={APP_NAME}\n"
        "Terminal=false\n"
        "Categories=Utility;Accessibility;\n"
        "Keywords=dictation;voice;speech;claude;transcribe;\n"
    )


def _applescript_string(text: str) -> str:
    """``text`` as a double-quoted AppleScript string."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mac_applet_source(command: str) -> str:
    """AppleScript that runs the app and stays open until it quits, which holds the permissions.

    Its exit status decides the dialog: quitting from the menu exits 0, so nothing shows.
    """
    shell = f"{MAC_ENVIRONMENT}exec {shlex.quote(command)} --open >/dev/null 2>&1"
    return (
        "try\n"
        f"    do shell script {_applescript_string(shell)}\n"
        "on error\n"
        f"    display dialog {_applescript_string(MAC_FAILED_MESSAGE)} "
        'buttons {"OK"} default button "OK"\n'
        "end try\n"
    )


def _build_mac_app(bundle: Path, command: str) -> None:
    """Before: nothing at ``bundle``. After: a signed applet with its own name and icon."""
    subprocess.run(
        ["osacompile", "-o", str(bundle), "-e", mac_applet_source(command)],
        check=True,
        capture_output=True,
    )
    plist_path = bundle / "Contents" / "Info.plist"
    with plist_path.open("rb") as file:
        info = plistlib.load(file)
    info.update(
        CFBundleName=DISPLAY_NAME,
        CFBundleDisplayName=DISPLAY_NAME,
        CFBundleIdentifier=MAC_BUNDLE_ID,
        CFBundleIconFile="applet",
        LSUIElement=True,  # lives in the menu bar: no Dock icon
        NSMicrophoneUsageDescription=MAC_MICROPHONE_REASON,  # without it, macOS refuses the mic
    )
    info.pop("CFBundleIconName", None)  # it would pick the stock icon from Assets.car
    with plist_path.open("wb") as file:
        plistlib.dump(info, file)
    resources = bundle / "Contents" / "Resources"
    (resources / "Assets.car").unlink(missing_ok=True)
    draw_icon("idle", MAC_ICON_SIZE).save(resources / "applet.icns", format="ICNS")
    # Editing the bundle broke its signature. "-" signs for this computer only (ad hoc),
    # which is all macOS needs to remember the permissions.
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
        check=True,
        capture_output=True,
    )


def install_mac_app() -> None:
    """Build the app beside its final place, so a failure leaves any earlier one untouched."""
    bundle = mac_app_path()
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=bundle.parent, prefix=f".{APP_NAME}.") as folder:
        built = Path(folder) / bundle.name
        try:
            _build_mac_app(built, app_command())
        except (OSError, subprocess.CalledProcessError) as error:
            sys.exit(f"Could not build the app: {error}")
        shutil.rmtree(bundle, ignore_errors=True)
        built.rename(bundle)
    print(f"Added the app: {bundle}")
    print("Open it with Spotlight, then allow what macOS asks for; the settings page guides you.")


def install_linux_launcher() -> None:
    icon = icon_path()
    icon.parent.mkdir(parents=True, exist_ok=True)
    draw_icon("idle", LAUNCHER_ICON_SIZE).save(icon)
    entry = desktop_file_path()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(desktop_entry(app_command()), encoding="utf-8")
    print(f"Added the app launcher: {entry}")


def install_launcher() -> None:
    if IS_MAC:
        install_mac_app()
    else:
        install_linux_launcher()


def uninstall_launcher() -> None:
    if IS_MAC:
        shutil.rmtree(mac_app_path(), ignore_errors=True)
    else:
        for path in (desktop_file_path(), icon_path()):
            path.unlink(missing_ok=True)
    print("Removed the app launcher.")
