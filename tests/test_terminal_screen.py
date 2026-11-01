"""Screen reading, checked against bytes recorded from the real Claude Code 2.1.309."""

import unittest
from pathlib import Path

from claude_dictation_anywhere import terminal_screen as ts

FIXTURES = Path(__file__).parent / "fixtures" / "claude_2.1.309"
ROWS, COLS = 24, 100


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def screen_of(data: bytes) -> tuple[str, ...]:
    return ts.TerminalScreen(ROWS, COLS).feed(data)


def frames(lines: list[str]) -> tuple[str, ...]:
    return tuple(lines) + ("",) * (ROWS - len(lines))


RULE = "─" * 100


class RecordedScreensTest(unittest.TestCase):
    def test_real_prompt_is_ready(self):
        self.assertTrue(ts.prompt_ready(screen_of(fixture("ready.raw"))))

    def test_real_menus_are_not_the_prompt(self):
        for name in ("trust_dialog.raw", "trust_yes_selected.raw", "signed_out_first_run.raw"):
            with self.subTest(name):
                self.assertFalse(ts.prompt_ready(screen_of(fixture(name))))

    def test_trust_dialog_says_where_the_arrow_is(self):
        self.assertEqual(ts.trust_dialog_choice(screen_of(fixture("trust_dialog.raw"))), "other")
        self.assertEqual(
            ts.trust_dialog_choice(screen_of(fixture("trust_yes_selected.raw"))), "yes"
        )
        self.assertIsNone(ts.trust_dialog_choice(screen_of(fixture("ready.raw"))))
        self.assertIsNone(ts.trust_dialog_choice(screen_of(fixture("signed_out_first_run.raw"))))

    def test_recording_shows_each_voice_state_in_order(self):
        # Bytes as they came, in small chunks: the states must appear in this order.
        data = fixture("record_cancel_record_collect.raw")
        screen = ts.TerminalScreen(ROWS, COLS)
        seen = []
        for start in range(0, len(data), 64):
            phase = ts.voice_phase(screen.feed(data[start : start + 64]))
            if phase and (not seen or seen[-1] != phase):
                seen.append(phase)
        self.assertEqual(seen, [ts.WARMUP, ts.LISTENING, ts.WARMUP, ts.LISTENING, ts.PROCESSING])

    def test_screen_ends_with_an_empty_ready_prompt_after_the_hand_off(self):
        rows = screen_of(fixture("record_cancel_record_collect.raw"))
        self.assertTrue(ts.prompt_ready(rows))
        self.assertIsNone(ts.voice_phase(rows))
        self.assertIn("❯", rows)


class SplitReadsTest(unittest.TestCase):
    def test_result_does_not_depend_on_how_reads_split_the_bytes(self):
        for name in sorted(path.name for path in FIXTURES.iterdir()):
            data = fixture(name)
            whole = screen_of(data)
            with self.subTest(name, split="every byte"):
                screen = ts.TerminalScreen(ROWS, COLS)
                for index in range(len(data)):
                    rows = screen.feed(data[index : index + 1])
                self.assertEqual(rows, whole)
            with self.subTest(name, split="every position"):
                for cut in range(1, min(len(data), 400)):
                    screen = ts.TerminalScreen(ROWS, COLS)
                    screen.feed(data[:cut])
                    self.assertEqual(screen.feed(data[cut:]), whole)

    def test_arrow_split_between_reads_survives(self):
        arrow = "❯".encode()
        screen = ts.TerminalScreen(ROWS, COLS)
        screen.feed(arrow[:1])
        self.assertTrue(screen.feed(arrow[1:])[0].startswith("❯"))

    def test_unsupported_sequence_split_between_reads_prints_nothing(self):
        for cut in range(1, len(ts.UNSUPPORTED_SEQUENCE)):
            screen = ts.TerminalScreen(ROWS, COLS)
            screen.feed(ts.UNSUPPORTED_SEQUENCE[:cut])
            rows = screen.feed(ts.UNSUPPORTED_SEQUENCE[cut:])
            self.assertEqual(rows[0], "", cut)

    def test_malformed_bytes_do_not_raise(self):
        rows = ts.TerminalScreen(ROWS, COLS).feed(b"\xff\xfe ok \x1b[999;999H\x1b[\xff")
        self.assertEqual(len(rows), ROWS)


class ErasedTextTest(unittest.TestCase):
    def test_erased_and_overwritten_text_is_gone(self):
        screen = ts.TerminalScreen(ROWS, COLS)
        self.assertIn("No speech detected", screen.feed(b"No speech detected")[0])
        self.assertEqual(screen.feed(b"\r\x1b[K")[0], "")
        screen.feed(b"listening")
        self.assertEqual(screen.feed(b"\rdone\x1b[K")[0], "done")

    def test_screen_stays_within_its_size_however_much_is_written(self):
        screen = ts.TerminalScreen(ROWS, COLS)
        rows = screen.feed(b"line\r\n" * 10_000)
        self.assertEqual(len(rows), ROWS)


class PromptShapesTest(unittest.TestCase):
    def test_wrapped_text_in_the_box_is_still_the_prompt(self):
        rows = frames([RULE, "❯ first line", "  second line", "  third line", RULE, "  footer"])
        self.assertTrue(ts.prompt_ready(rows))

    def test_menu_shapes_are_refused(self):
        for lines in (
            ["❯ 1. Yes", "  2. No"],
            [RULE, "❯ 1. Yes", RULE],  # numbered, even between rules
            [RULE, "❯ Yes", RULE, " Enter to confirm · Esc to cancel"],
            ["❯ hello", RULE],  # no rule above
            [RULE, "❯ hello"],  # no rule below
        ):
            with self.subTest(lines):
                self.assertFalse(ts.prompt_ready(frames(lines)))

    def test_trust_dialog_with_changed_wording_is_not_answered(self):
        rows = frames(["Quick safety check", " ❯ Nope", "   Sure, trust it"])
        self.assertIsNone(ts.trust_dialog_choice(rows))

    def test_transcript_words_are_not_voice_states_or_errors(self):
        rows = frames(
            [
                RULE,
                "❯ I am listening… No speech detected, keep holding, Voice: processing…",
                RULE,
                "  0 tokens",
            ]
        )
        self.assertIsNone(ts.voice_phase(rows))
        self.assertNotIn("No speech", ts.status_text(rows))

    def test_notices_above_and_below_the_box_count_as_status(self):
        rows = frames(["top notice", RULE, "❯", RULE, "  bottom notice"])
        status = ts.status_text(rows)
        self.assertIn("top notice", status)
        self.assertIn("bottom notice", status)

    def test_long_text_that_pushed_the_upper_rule_off_is_not_status(self):
        rows = frames(["  transcript words", "  No speech detected in my text", RULE, "  footer"])
        self.assertEqual(ts.status_text(rows).strip(), "footer")

    def test_prompt_box_text_reads_wrapped_rows_and_nothing_else(self):
        rows = frames(["banner", RULE, "❯ first part of it", "  second part", RULE, "  footer"])
        self.assertEqual(ts.prompt_box_text(rows), "first part of it second part")
        self.assertEqual(ts.prompt_box_text(frames([RULE, "❯", RULE])), "")
        self.assertEqual(ts.prompt_box_text(frames(["no box here"])), "")

    def test_prompt_box_text_with_the_upper_rule_scrolled_off(self):
        rows = frames(["  the end of a long text", RULE, "  footer"])
        self.assertEqual(ts.prompt_box_text(rows), "the end of a long text")

    def test_real_recording_leaves_its_text_readable_before_the_hand_off(self):
        data = fixture("record_cancel_record_collect.raw")
        screen = ts.TerminalScreen(ROWS, COLS)
        for start in range(0, len(data), 32):
            rows = screen.feed(data[start : start + 32])
            if ts.voice_phase(rows) is None and "1, 2, 3." in ts.prompt_box_text(rows):
                break  # the moment the session reads it, just before Ctrl+G
        self.assertEqual(ts.prompt_box_text(rows), "Testing the transcript signal. 1, 2, 3.")

    def test_whitespace_is_ignored_when_matching(self):
        self.assertTrue(ts.shows("trustthis\nfolder", "trust this folder"))
        self.assertFalse(ts.shows("something else", "trust this folder"))


if __name__ == "__main__":
    unittest.main()
