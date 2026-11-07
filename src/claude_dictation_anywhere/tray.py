"""Tray / menu-bar icon: a microphone whose colour shows the state, plus a small menu.

Linux uses AppIndicator (GNOME needs the AppIndicator extension, which most
distributions ship); macOS uses the menu bar.
"""

import threading

import pystray

from .hotkeys import describe_shortcut
from .icon import draw_icon
from .settings import IS_MAC

if IS_MAC:
    from PyObjCTools.AppHelper import callAfter

STATE_TITLES = {
    "idle": "Ready",
    "starting": "Starting Claude Code…",
    "listening": "Listening…",
    "transcribing": "Transcribing…",
    "paused": "Paused",
    "capturing": "Press the new shortcut…",
    "failed": "Stopped working; restart the app",
}


class TrayIcon:
    """Wraps pystray. ``run()`` blocks until ``stop()``; on macOS call it on the main thread."""

    def __init__(self, controller, open_settings, quit_app):
        self.controller = controller
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: STATE_TITLES.get(controller.state, ""), None, enabled=False),
            pystray.MenuItem(
                lambda _: "Shortcut: " + describe_shortcut(controller.settings["shortcut_key"]),
                None,
                enabled=False,
            ),
            pystray.MenuItem("Settings and history…", lambda: open_settings(), default=True),
            pystray.MenuItem(
                "Pause shortcut",
                lambda: controller.set_paused(not controller.paused),
                checked=lambda _: controller.paused,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: quit_app()),
        )
        self.icon = pystray.Icon(
            "claude-dictation-anywhere",
            draw_icon("idle"),
            "Claude Dictation Anywhere",
            menu,
        )
        controller.on_change.append(self.refresh)

    def refresh(self, controller) -> None:
        if IS_MAC and threading.current_thread() is not threading.main_thread():
            callAfter(self.refresh, controller)
            return
        self.icon.icon = draw_icon(controller.state)
        self.icon.title = f"Claude Dictation Anywhere: {STATE_TITLES.get(controller.state, '')}"
        self.icon.update_menu()

    def run(self) -> None:
        if IS_MAC:
            # pystray runs setup on a worker; AppKit visibility belongs on the main loop.
            self.icon.run(setup=lambda icon: callAfter(setattr, icon, "visible", True))
        else:
            self.icon.run()

    def stop(self) -> None:
        if IS_MAC and threading.current_thread() is not threading.main_thread():
            callAfter(self.stop)
            return
        self.icon.stop()
