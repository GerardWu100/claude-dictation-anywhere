"""Tray / menu-bar icon: a microphone whose colour shows the state, plus a small menu.

Linux uses AppIndicator (GNOME needs the AppIndicator extension, which most
distributions ship); macOS uses the menu bar.
"""

import threading

import pystray
from PIL import Image, ImageDraw

from .settings import IS_MAC

if IS_MAC:
    from PyObjCTools.AppHelper import callAfter

ICON_SIZE = 64
# RGB colour of the round badge for each state.
STATE_COLOURS = {
    "idle": (217, 119, 87),  # Claude orange: ready
    "starting": (230, 170, 40),  # amber: hidden Claude Code is starting
    "listening": (220, 40, 40),  # red: recording
    "transcribing": (60, 120, 220),  # blue: waiting for the text
    "paused": (140, 140, 140),  # grey: shortcut ignored
    "capturing": (150, 90, 210),  # purple: waiting for a new shortcut
}
STATE_TITLES = {
    "idle": "Ready",
    "starting": "Starting Claude Code…",
    "listening": "Listening…",
    "transcribing": "Transcribing…",
    "paused": "Paused",
    "capturing": "Press the new shortcut…",
}


def draw_icon(state: str, size: int = ICON_SIZE) -> Image.Image:
    """A white microphone on a coloured circle, drawn on a 64-unit grid scaled to ``size`` px."""
    scale = size / ICON_SIZE

    def box(*units):
        return tuple(round(unit * scale) for unit in units)

    line_width = max(1, round(4 * scale))
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse(box(2, 2, 62, 62), fill=STATE_COLOURS.get(state, STATE_COLOURS["idle"]))
    white = (255, 255, 255, 255)
    draw.rounded_rectangle(box(25, 13, 39, 37), radius=round(7 * scale), fill=white)  # capsule
    draw.arc(box(19, 22, 45, 45), start=0, end=180, fill=white, width=line_width)  # cradle
    draw.line(box(32, 45, 32, 51), fill=white, width=line_width)  # stem
    draw.line(box(24, 51, 40, 51), fill=white, width=line_width)  # base
    return image


class TrayIcon:
    """Wraps pystray. ``run()`` blocks until ``stop()``; on macOS call it on the main thread."""

    def __init__(self, controller, open_settings, quit_app):
        self.controller = controller
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: STATE_TITLES.get(controller.state, ""), None, enabled=False),
            pystray.MenuItem("Settings and history…", lambda: open_settings(), default=True),
            pystray.MenuItem(
                "Pause shortcut",
                lambda: controller.set_paused(controller.state != "paused"),
                checked=lambda _: controller.state == "paused",
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
