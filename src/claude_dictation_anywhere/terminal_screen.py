"""What the hidden Claude Code is showing right now, read from its terminal output.

Terminal output is not text to search: it moves the cursor, overwrites cells, and
erases lines, and any read can stop in the middle of a character or an escape
code. ``TerminalScreen`` feeds the bytes to pyte (a terminal emulator library),
which keeps that state between reads, so the result does not depend on how the
output was split, and text that was erased is really gone.

The functions below read the rows. Layout as recorded from Claude Code 2.1.309
(fixtures in tests/fixtures/):

     8 ──────────────────────────────  rule above the prompt box
     9 ❯ the transcript goes here      prompt row; long text wraps below it
    10 ──────────────────────────────  rule below the prompt box
    11   ... · keep holding…           footer, where voice shows its state

A selection menu also draws "❯", but never directly under a rule, so a menu is not
mistaken for the prompt: not the trust dialog ("❯ No, exit"), nor the signed-out
first-run theme picker ("❯ ✔ Dark mode").
"""

import re

import pyte

PROMPT_ARROW = "❯"
RULE_CHAR = "─"
MIN_RULE_CHARS = 20
# Shown under selection menus; the prompt has neither.
MENU_HINTS = ("Enter to confirm", "Esc to cancel")
NUMBERED_OPTION = re.compile(PROMPT_ARROW + r"\s*\d+\.")

TRUST_QUESTION = "Quick safety check"
TRUST_YES_OPTION = "Yes, I trust this folder"

# Footer text per voice state. The first two are in Claude Code's voice docs; the
# third was observed from release until the final text is in the prompt box.
WARMUP = "keep holding"
LISTENING = "listening"
PROCESSING = "Voice: processing"
VOICE_PHASES = (WARMUP, LISTENING, PROCESSING)

# Sent by Claude Code when it leaves an editor ("pop keyboard mode"). pyte does not
# know "<" in an escape code and would print the "u" on the screen.
UNSUPPORTED_SEQUENCE = b"\x1b[<u"


class TerminalScreen:
    """A fake terminal: ``feed`` it output bytes, get back the visible rows.

    Parameters
    ----------
    rows, cols : int
        Screen size; must match the size set on the pty.
    """

    def __init__(self, rows: int, cols: int):
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._held_back = b""

    def feed(self, chunk: bytes) -> tuple[str, ...]:
        """Take the next output bytes; return every row with trailing spaces removed."""
        data = self._held_back + chunk
        data = data.replace(UNSUPPORTED_SEQUENCE, b"")
        # A chunk may end part-way through the sequence: keep that part for the next one.
        keep = next(
            (n for n in range(len(UNSUPPORTED_SEQUENCE) - 1, 0, -1)
             if data.endswith(UNSUPPORTED_SEQUENCE[:n])),
            0,
        )  # fmt: skip
        self._held_back = data[len(data) - keep :] if keep else b""
        self._stream.feed(data[: len(data) - keep] if keep else data)
        return tuple(row.rstrip() for row in self._screen.display)


def shows(text: str, marker: str) -> bool:
    """True if ``marker`` is in ``text``, ignoring all whitespace.

    Claude Code draws most gaps between words as cursor moves. The emulator turns
    those back into spaces, but line wrapping can still split a phrase.
    """
    return re.sub(r"\s", "", marker) in re.sub(r"\s", "", text)


def _is_rule(row: str) -> bool:
    stripped = row.strip()
    return len(stripped) >= MIN_RULE_CHARS and set(stripped) == {RULE_CHAR}


def _rule_rows(rows: tuple[str, ...]) -> list[int]:
    return [index for index, row in enumerate(rows) if _is_rule(row)]


def prompt_ready(rows: tuple[str, ...]) -> bool:
    """True only for the real prompt box: the arrow row directly under a rule, with one below.

    The lower rule need not be the next row, because text in the box may wrap.
    """
    if any(shows(row, hint) for row in rows for hint in MENU_HINTS):
        return False
    return any(
        row.startswith(PROMPT_ARROW)
        and not NUMBERED_OPTION.match(row)
        and index > 0
        and _is_rule(rows[index - 1])
        and any(_is_rule(below) for below in rows[index + 1 :])
        for index, row in enumerate(rows)
    )


def prompt_box_text(rows: tuple[str, ...]) -> str:
    """The text visible in the prompt box, as a last resort when the hand-off fails.

    Only what fits on the screen, with line wraps turned into spaces, so it may be
    incomplete. It is offered for copying and never typed.
    """
    rules = _rule_rows(rows)
    if not rules:
        return ""
    upper = rules[-2] if len(rules) >= 2 else -1
    box = [row.strip() for row in rows[upper + 1 : rules[-1]]]
    if box and box[0].startswith(PROMPT_ARROW):
        box[0] = box[0].removeprefix(PROMPT_ARROW).strip()
    return " ".join(row for row in box if row)


def footer_text(rows: tuple[str, ...]) -> str:
    """Everything below the prompt box. Never the transcript, which may say "listening"."""
    rules = _rule_rows(rows)
    return "\n".join(rows[rules[-1] + 1 :]) if rules else ""


def status_text(rows: tuple[str, ...]) -> str:
    """Everything except the prompt box, for error notices.

    Without this, dictating the words "no speech detected" would look like that
    error. A long transcript can push the upper rule off the screen; then only
    the footer is left.
    """
    rules = _rule_rows(rows)
    if len(rules) >= 2:
        return "\n".join(rows[: rules[-2]] + rows[rules[-1] + 1 :])
    return footer_text(rows) if rules else "\n".join(rows)


def voice_phase(rows: tuple[str, ...]) -> str | None:
    """``WARMUP``, ``LISTENING``, or ``PROCESSING`` from the footer; None when voice is idle."""
    footer = footer_text(rows)
    return next((phase for phase in VOICE_PHASES if shows(footer, phase)), None)


def trust_dialog_choice(rows: tuple[str, ...]) -> str | None:
    """Read the first-run trust dialog.

    Returns
    -------
    str or None
        "yes" if the arrow is on "Yes, I trust this folder", "other" if it is on
        the other option, None if this is not exactly that dialog. Anything
        unrecognised is None, so the caller never answers a dialog it cannot read.
    """
    if not any(shows(row, TRUST_QUESTION) for row in rows):
        return None
    options = [row.strip() for row in rows if row.strip().endswith(TRUST_YES_OPTION)]
    selected = [row.strip() for row in rows if row.strip().startswith(PROMPT_ARROW)]
    if len(options) != 1 or len(selected) != 1:
        return None
    return "yes" if selected == options else "other"
