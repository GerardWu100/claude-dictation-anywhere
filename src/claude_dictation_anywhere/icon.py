"""The app's icon: a white microphone on a circle coloured by state. No desktop services needed."""

from PIL import Image, ImageDraw

ICON_SIZE = 64
# RGB colour of the round badge for each state.
STATE_COLOURS = {
    "idle": (217, 119, 87),  # Claude orange: ready
    "starting": (230, 170, 40),  # amber: hidden Claude Code is starting
    "listening": (220, 40, 40),  # red: recording
    "transcribing": (60, 120, 220),  # blue: waiting for the text
    "paused": (140, 140, 140),  # grey: shortcut ignored
    "capturing": (150, 90, 210),  # purple: waiting for a new shortcut
    "failed": (90, 60, 60),  # dark: the dictation worker has stopped; restart the app
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
