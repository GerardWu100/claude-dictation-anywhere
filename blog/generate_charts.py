"""Draw the blog post's images: the dictation flow diagram and the cover banner.

Run from anywhere:

    uv run --with matplotlib --with pillow python blog/generate_charts.py

Outputs (overwritten each run), in ``blog/images/``:

    dictation-flow.png   one dictation, from key press to cursor, in two lanes
    cover.png            cover image for the post's ``image:`` frontmatter

The timings printed on the flow diagram are single measurements recorded in the
project's TRAIL_NOTES.md (warm-up, release -> "Voice: processing…" -> final text)
and in the ``EDITOR_TIMEOUT_S`` comment of ``claude_session.py``. They are not
benchmarks; edit ``TIMING_NOTES`` if new measurements replace them.
"""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

BLOG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BLOG_DIR.parent
IMAGES_DIR = BLOG_DIR / "images"
sys.path.insert(0, str(PROJECT_DIR / "src"))

from claude_dictation_anywhere.icon import draw_icon  # noqa: E402  (needs the path above)

DPI = 200
BANNER_DPI = 100  # 16x6 in figure -> 1600x600, the size every post banner uses
CLAUDE_ORANGE = "#d97757"
HIDDEN_SLATE = "#3d3d3a"
PAGE_BACKGROUND = "#faf9f5"
DARK_BACKGROUND = "#1f1e1d"
MUTED_TEXT = "#6b6a66"
ARROW_COLOUR = "#4a4a46"
FONT = "DejaVu Sans"

# Box geometry in data units of the flow diagram (x: 0..16, y: 0.4..6.9).
BOX_WIDTH, BOX_HEIGHT = 2.75, 1.25
DESKTOP_ROW_Y, HIDDEN_ROW_Y = 4.85, 1.9
COLUMN_X = (1.85, 4.95, 8.05, 11.15, 14.25)

# (column, label) for each lane.
DESKTOP_BOXES = (
    (0, "Hold the\nshortcut"),
    (2, "Let go"),
    (4, "Paste or type\nat the cursor"),
)
HIDDEN_BOXES = (
    (0, "Write a Space\nevery 30 ms"),
    (1, "/voice\nrecords"),
    (2, "Footer shows\n“Voice: processing…”"),
    (3, "Final text in the\nprompt box, unsent"),
    (4, "Ctrl+G: editor hook\nhands the text over"),
)
# Column -> measured timing printed under that hidden-lane box.
TIMING_NOTES = {
    1: "“keep holding…” 0.06 s\n“listening” 0.15 s\nafter the first Space",
    2: "0.18 s after release",
    3: "0.44 s after release",
    4: "usually about 0.3 s",
}


def draw_box(axes, centre_x: float, centre_y: float, label: str, colour: str) -> None:
    """Rounded box with white centred text."""
    axes.add_patch(
        FancyBboxPatch(
            (centre_x - BOX_WIDTH / 2, centre_y - BOX_HEIGHT / 2),
            BOX_WIDTH,
            BOX_HEIGHT,
            boxstyle="round,pad=0.02,rounding_size=0.18",
            facecolor=colour,
            edgecolor="none",
        )
    )
    axes.text(centre_x, centre_y, label, ha="center", va="center", color="white",
              fontsize=13, fontweight="bold", family=FONT, linespacing=1.3)  # fmt: skip


def draw_arrow(axes, start: tuple[float, float], end: tuple[float, float]) -> None:
    axes.add_patch(
        FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=18,
                        color=ARROW_COLOUR, linewidth=1.8, shrinkA=0, shrinkB=0)  # fmt: skip
    )


def draw_flow_diagram(path: Path) -> None:
    """Two lanes: what the user does (top) and what the hidden Claude Code does (bottom)."""
    figure, axes = plt.subplots(figsize=(16, 6.6), constrained_layout=True)
    figure.patch.set_facecolor(PAGE_BACKGROUND)
    axes.set_facecolor(PAGE_BACKGROUND)
    axes.set_xlim(0, 16)
    axes.set_ylim(0.4, 6.9)
    axes.axis("off")
    lane_label_x = COLUMN_X[0] - BOX_WIDTH / 2
    axes.text(lane_label_x, 6.55, "One dictation, from key press to cursor", fontsize=20,
              fontweight="bold", family=FONT, va="center")  # fmt: skip
    # Desktop label above its lane; hidden label below its lane, clear of the press arrow.
    axes.text(lane_label_x, DESKTOP_ROW_Y + BOX_HEIGHT / 2 + 0.3, "YOUR DESKTOP", fontsize=11,
              color=MUTED_TEXT, fontweight="bold", family=FONT)  # fmt: skip
    axes.text(lane_label_x, HIDDEN_ROW_Y - BOX_HEIGHT / 2 - 0.2, "HIDDEN CLAUDE CODE",
              fontsize=11, color=MUTED_TEXT, fontweight="bold", family=FONT, va="top")  # fmt: skip

    for column, label in DESKTOP_BOXES:
        draw_box(axes, COLUMN_X[column], DESKTOP_ROW_Y, label, CLAUDE_ORANGE)
    for column, label in HIDDEN_BOXES:
        draw_box(axes, COLUMN_X[column], HIDDEN_ROW_Y, label, HIDDEN_SLATE)

    # Along the hidden lane: each stage leads to the next.
    for left, right in zip(COLUMN_X, COLUMN_X[1:]):
        draw_arrow(axes, (left + BOX_WIDTH / 2, HIDDEN_ROW_Y), (right - BOX_WIDTH / 2, HIDDEN_ROW_Y))
    # Between lanes: press starts the Spaces, release stops them, the hand-off feeds delivery.
    top_edge = HIDDEN_ROW_Y + BOX_HEIGHT / 2
    bottom_edge = DESKTOP_ROW_Y - BOX_HEIGHT / 2
    draw_arrow(axes, (COLUMN_X[0], bottom_edge), (COLUMN_X[0], top_edge))
    draw_arrow(axes, (COLUMN_X[2], bottom_edge), (COLUMN_X[2], top_edge))
    draw_arrow(axes, (COLUMN_X[4], top_edge), (COLUMN_X[4], bottom_edge))
    axes.text(COLUMN_X[0] + 0.12, (top_edge + bottom_edge) / 2, "at once", fontsize=11,
              color=MUTED_TEXT, va="center", family=FONT)  # fmt: skip
    axes.text(COLUMN_X[2] + 0.12, (top_edge + bottom_edge) / 2, "Spaces stop", fontsize=11,
              color=MUTED_TEXT, va="center", family=FONT)  # fmt: skip
    axes.text(COLUMN_X[4] + 0.12, (top_edge + bottom_edge) / 2, "clipboard, then\nShift+Insert or Cmd+V",
              fontsize=11, color=MUTED_TEXT, va="center", family=FONT)  # fmt: skip

    for column, note in TIMING_NOTES.items():
        axes.text(COLUMN_X[column], HIDDEN_ROW_Y - BOX_HEIGHT / 2 - 0.2, note, ha="center",
                  va="top", fontsize=11, color=MUTED_TEXT, family=FONT, linespacing=1.3)  # fmt: skip

    figure.savefig(path, dpi=DPI, facecolor=figure.get_facecolor())
    plt.close(figure)


def draw_banner(path: Path) -> None:
    """Cover image: the app's tray icon, its name on two lines, and the recording-state icons.

    The site shows covers as small cards (about 330 px wide, cropped), so the name is set
    on two lines in a large font and the state icons are big enough to read at that size.
    Layout is in data units (x: 0..16, y: 0..6); saved at BANNER_DPI -> 1600x600 pixels.
    """
    figure, axes = plt.subplots(figsize=(16, 6))
    figure.subplots_adjust(0, 0, 1, 1)  # no margins: the axes fill the whole banner
    figure.patch.set_facecolor(DARK_BACKGROUND)
    axes.set_xlim(0, 16)
    axes.set_ylim(0, 6)
    axes.axis("off")
    main_icon = draw_icon("idle", size=1024)
    axes.imshow(main_icon, extent=(0.8, 4.8, 1.0, 5.0), aspect="auto", zorder=2,
                interpolation="lanczos")  # fmt: skip
    axes.text(5.6, 3.75, "Claude Dictation\nAnywhere", fontsize=60, fontweight="bold",
              color="white", va="center", family=FONT, linespacing=1.1)  # fmt: skip
    for index, state in enumerate(("starting", "listening", "transcribing")):
        left = 5.65 + index * 1.35
        axes.imshow(draw_icon(state, size=256), extent=(left, left + 1.05, 0.85, 1.9),
                    aspect="auto", zorder=2, interpolation="lanczos")  # fmt: skip
    figure.savefig(path, dpi=BANNER_DPI, facecolor=figure.get_facecolor())
    plt.close(figure)


def main() -> None:
    IMAGES_DIR.mkdir(exist_ok=True)
    draw_flow_diagram(IMAGES_DIR / "dictation-flow.png")
    draw_banner(IMAGES_DIR / "cover.png")
    print(f"wrote {IMAGES_DIR / 'dictation-flow.png'} and {IMAGES_DIR / 'cover.png'}")


if __name__ == "__main__":
    main()
