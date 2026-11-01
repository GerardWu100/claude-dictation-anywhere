"""Drive a hidden copy of the Claude Code installed on this system, only for its /voice dictation.

Claude Code runs in a pseudo-terminal (pty: a fake terminal a program can
drive), and this module works it the way a person would:

    hold    -> write " " every 30 ms; Claude Code reads fast-repeating spaces as
               a held Space bar and records the microphone (warm-up ~0.13 s)
    release -> stop the spaces; Claude Code finishes the transcript and puts it in
               its prompt box, unsent. Its footer shows when that is done.
    collect -> press Ctrl+G ("edit prompt in external editor"); the editor is a
               tiny hook script that hands the text over and empties the box
    cancel  -> press Esc, Claude Code's documented way to discard a recording

No prompt is ever sent to the model, so no tokens are used, and the Claude.ai
login never leaves Claude Code. The app does not bundle Claude Code: it finds
the one already installed (see ``find_claude``). Enter is only ever written to
answer the exact first-run trust dialog, after reading that the arrow is on
"Yes"; a screen this module does not recognise is never answered.

Threads: one reader thread drains the pty all the time, because a terminal
program blocks once nobody reads its output (and on macOS it cannot even finish
exiting). That thread alone closes the pty, so no other thread can read a file
descriptor number that was closed and reused. All other threads only write, and
the pty is non-blocking, so a frozen Claude Code cannot block them.
"""

import fcntl
import json
import logging
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
from pathlib import Path

from .settings import PRIVATE_DIR_MODE, data_dir, runtime_dir, write_private_file
from .terminal_screen import (
    LISTENING,
    PROCESSING,
    WARMUP,
    TerminalScreen,
    prompt_box_text,
    prompt_ready,
    shows,
    status_text,
    trust_dialog_choice,
    voice_phase,
)

# A small fake screen gives Claude Code less to redraw.
PTY_ROWS, PTY_COLS = 24, 100
# Terminals repeat a held key about every 30 ms; Claude Code detects "held"
# from that rhythm, so the fake Space must repeat at least this fast.
KEY_REPEAT_INTERVAL_S = 0.03

# Deadlines, one per stage, so a slow stage is reported as what it is.
STARTUP_TIMEOUT_S = 25
FINALIZE_TIMEOUT_S = 12  # release -> final text in the prompt box
WARMUP_FREEZE_TIMEOUT_S = 3  # release -> "keep holding…" must be gone
EDITOR_TIMEOUT_S = 5  # Ctrl+G -> the hook has handed over the text (usually 0.3 s)
POST_EDITOR_TIMEOUT_S = 2  # hook done -> the prompt box is back
CANCEL_TIMEOUT_S = 2  # Esc -> voice is idle again
TRUST_KEY_TIMEOUT_S = 1  # arrow key -> the dialog shows it moved
READY_GRACE_S = 0.3  # press -> the prompt box, if a redraw is under way
WRITE_TIMEOUT_S = 1  # a control key waits this long for room in a full pty
# The footer can change from "listening…" to "Voice: processing…" across two reads;
# only this long without either counts as finished.
FINAL_SETTLE_S = 0.15
# Without the "processing" signal (nothing was said, or a future Claude Code words
# it differently), the old rule applies: the screen has stopped changing.
SCREEN_QUIET_S = 0.6
STARTUP_QUIET_S = 0.3  # a prompt that has been stable this long is ready
POLL_S = 0.02
READER_POLL_S = 0.2  # how often the reader thread notices stop() on a silent pty
STOP_GRACE_S = 0.5  # after SIGTERM, then after SIGKILL
READER_JOIN_TIMEOUT_S = 1

CTRL_G = b"\x07"
ESCAPE = b"\x1b"
SPACE = b" "
ARROW_DOWN = b"\x1b[B"
ENTER = b"\r"
# Model for the hidden session. Dictation never sends a prompt; the cheapest
# model only limits the cost if something ever did.
HIDDEN_SESSION_MODEL = "haiku"
# The version the screen layout and footer words were recorded from (tests/fixtures/).
TESTED_CLAUDE_VERSION = "2.1.309"

# Where Claude Code is usually installed when `claude` is not on PATH (a tray
# app started from the desktop often has a shorter PATH than a terminal).
CLAUDE_CANDIDATE_PATHS = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "/usr/bin/claude",
    "~/.npm-global/bin/claude",
)

LOGGED_OUT_MARKER = "Not logged in"
# Messages Claude Code shows when dictation fails (from its voice docs).
VOICE_ERROR_MARKERS = (
    LOGGED_OUT_MARKER,
    "Voice mode requires",
    "Voice mode is disabled",
    "Voice mode could not",
    "Microphone access is denied",
    "Voice input is failing",
    "No audio detected",
    "No speech detected",
    "Voice connection failed",
    "Voice stream error",
    "Dictation error",
)
UNKNOWN_OPTION_MARKER = "unknown option"
# Claude Code types the first couple of repeated spaces before it notices the
# hold and deletes them; more left over than this means it never recorded.
MAX_WARMUP_SPACES = 5
LOGGED_OUT_HELP = (
    "Claude Code is not logged in. Run `claude` in a terminal, type /login, "
    "and sign in with your Claude.ai account."
)
FROZEN_AT_MICROPHONE_HELP = (
    "Claude Code froze before recording, usually while waiting for microphone permission. "
    "Answer the macOS prompt if one is open, or allow the app that started this one under "
    "System Settings → Privacy & Security → Microphone. The next press restarts Claude Code."
)
NO_VOICE_HELP = (
    "Recording never started (Claude Code typed the spaces instead). "
    "Usually it is not logged in: run `claude`, type /login, then try /voice once."
)
UNKNOWN_SCREEN_HELP = (
    "Claude Code is showing a screen this app does not recognise, so it pressed nothing. "
    "Run `claude` in a terminal, answer what it asks (sign-in, first-run questions), and "
    "quit it; the next press starts again. If that does not help, a Claude Code update may "
    f"have changed its screen (this app was checked with version {TESTED_CLAUDE_VERSION})."
)
OLD_VERSION_HELP = (
    "This Claude Code does not support the options this app needs. Update it: `claude update`."
)
HOOK_FAILED_HELP = "Claude Code did not hand over the text (Ctrl+G got no answer)."
UNFINISHED_HELP = "Claude Code did not finish the transcript in time; this is the text so far."
STOPPED_HELP = "The hidden Claude Code stopped."

# Flags for the hidden session, all standard Claude Code options. Safe mode
# skips your CLAUDE.md, hooks, plugins, skills, and MCP servers, so none of your
# own setup runs in it; without it, startup also stops at dialogs such as
# "Claude in Chrome detected". Your organization's managed policy still applies,
# as it should. No MCP config and no slash commands save another ~4 MB. No tools:
# even a prompt sent by mistake could not touch files or run commands.
CLAUDE_BASE_FLAGS = [
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--tools",
    "",
]

# Variables that say which configuration or network path this user chose. They are
# not credentials, and dropping one would make a correctly set-up login look missing.
KEPT_CLAUDE_VARIABLES = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_CLIENT_CERT",
        "CLAUDE_CODE_CLIENT_KEY",
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    }
)

# Stand-in "text editor" Claude Code runs on Ctrl+G. Contract, in order:
#   1. copy the prompt file (the last argument) to a partial file in the session's
#      private folder, and stop with an error if that fails;
#   2. publish it under this recording's generation number with an atomic rename;
#   3. only now empty the prompt file, which clears the prompt box;
#   4. write the "done" file, which tells the app the box is cleared.
# With `set -eu`, a failure at any step leaves every earlier copy in place, so no
# failure can erase the only copy of the text.
EDITOR_HOOK_SCRIPT = """#!/bin/sh
set -eu
umask 077
for prompt_file in "$@"; do :; done
generation=$(cat "$CDA_HANDOFF_DIR/generation")
cp "$prompt_file" "$CDA_HANDOFF_DIR/result.$generation.partial"
mv "$CDA_HANDOFF_DIR/result.$generation.partial" "$CDA_HANDOFF_DIR/result.$generation"
: > "$prompt_file"
: > "$CDA_HANDOFF_DIR/done.$generation"
"""
HOOK_FILE_NAME = "editor_hook.sh"
HANDOFF_DIR_PREFIX = "session-"

log = logging.getLogger(__name__)


class DictationError(RuntimeError):
    """A problem the user must fix or retry (not logged in, no microphone, ...).

    Messages are safe to log: they never contain terminal output or transcript text.
    """


class PartialTranscript(DictationError):
    """Some text exists but may be incomplete; ``text`` should be offered, not typed blindly."""

    def __init__(self, message: str, text: str):
        super().__init__(message)
        self.text = text


def remove_stale_handoffs() -> None:
    """Delete hand-off folders of earlier runs, which may hold transcripts.

    Call only while holding the single-instance lock, so none of them is in use.
    """
    for folder in runtime_dir().glob(f"{HANDOFF_DIR_PREFIX}*"):
        shutil.rmtree(folder, ignore_errors=True)


def find_claude(configured_path: str = "") -> str | None:
    """Path of the Claude Code program installed on this system, or None.

    Order: the path set in settings, then `claude` on PATH, then the usual
    install locations in ``CLAUDE_CANDIDATE_PATHS``.
    """
    if configured_path:
        try:
            path = Path(configured_path).expanduser()
            return str(path) if os.access(path, os.X_OK) else None
        except (RuntimeError, ValueError):  # "~nobody/x", or a null byte: not a usable path
            return None
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    for candidate in CLAUDE_CANDIDATE_PATHS:
        path = Path(candidate).expanduser()
        if os.access(path, os.X_OK):
            return str(path)
    return None


def claude_version(claude_path: str) -> str:
    """Version string from `claude --version`, e.g. "2.1.310"; "" if it cannot be read."""
    try:
        result = subprocess.run(
            [claude_path, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = re.search(r"\d+\.\d+\.\d+", result.stdout)
    return match.group(0) if match else ""


def build_claude_env(base_env: dict[str, str], hook: Path, handoff_dir: Path) -> dict[str, str]:
    """Environment for the hidden session.

    ``CLAUDE*`` / ``ANTHROPIC*`` variables are dropped: started from inside another
    Claude Code session, those point the child at the parent's login or proxy, and
    an ``ANTHROPIC_API_KEY`` turns dictation off entirely (it needs a Claude.ai
    login). ``KEPT_CLAUDE_VARIABLES`` are the exception.
    """
    env = {
        k: v
        for k, v in base_env.items()
        if k in KEPT_CLAUDE_VARIABLES or not k.startswith(("CLAUDE", "ANTHROPIC"))
    }
    env.update(
        TERM="xterm-256color",
        EDITOR=str(hook),
        VISUAL=str(hook),
        CDA_HANDOFF_DIR=str(handoff_dir),
        # Standard Claude Code switch for the auto-updater, telemetry, error reports,
        # and the feedback command: never updates itself mid-dictation, and saves
        # ~16 MB (PSS 184 -> 168 MB measured). Dictation still works with it.
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    return env


def build_claude_args(claude_path: str, language: str) -> list[str]:
    """Command line for the hidden session: minimal mode plus hold-to-record voice settings."""
    settings = {"voice": {"enabled": True, "mode": "hold", "autoSubmit": False}}
    if language:
        settings["language"] = language
    return [
        claude_path,
        *CLAUDE_BASE_FLAGS,
        "--model",
        HIDDEN_SESSION_MODEL,
        "--settings",
        json.dumps(settings),
    ]


class ClaudeVoiceSession:
    """One hidden interactive Claude Code session, used only for its dictation.

    Parameters
    ----------
    claude_path : str
        Program to run (from ``find_claude``).
    language : str
        Dictation language code, or "" for Claude Code's default (English).

    Notes
    -----
    ``rows`` is the live screen, replaced as a whole by the reader thread, so
    reading it needs no lock. Between ``begin_hold`` and the end of
    ``collect_transcript`` the reader also records which voice states and error
    notices appeared, because a notice may be gone again by the time anyone looks.

    ``stop`` may be called from any thread, at any moment, repeatedly; it makes a
    ``start`` or ``collect_transcript`` in progress raise ``DictationError``.
    """

    def __init__(self, claude_path: str, language: str):
        self.claude_path = claude_path
        self.language = language
        self.pid: int | None = None
        self.fd: int | None = None
        self.rows: tuple[str, ...] = ()
        self.last_output_at = 0.0
        self.workdir = data_dir() / "dictation"  # neutral name: Claude Code uses it as a word hint
        self.handoff_dir: Path | None = None
        self._generation = 0
        self._lifecycle_lock = threading.RLock()  # pid and fd changes, and fork against stop
        self._observe_lock = threading.Lock()  # what the reader records for the current hold
        self._phases_seen: set[str] = set()
        self._phase_left_at = 0.0
        self._failures: list[str] = []  # error notices seen since begin_hold, oldest first
        self._ignored_failures: set[str] = set()  # still on screen from before begin_hold
        self._stopping = threading.Event()
        self._reader_stop = threading.Event()
        self._reader: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Last known state, without touching the process: cheap enough for status polls."""
        return self.pid is not None

    def is_alive(self) -> bool:
        """True while Claude Code runs; collects the exit status of one that has ended."""
        with self._lifecycle_lock:
            if self.pid is None:
                return False
            try:
                finished_pid, _ = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                finished_pid = self.pid
            if finished_pid == self.pid:
                self.pid = None
                return False
            return True

    def start(self) -> None:
        """Start Claude Code in a pty and wait until its prompt box is ready.

        Raises
        ------
        DictationError
            If Claude Code is not logged in, exits, is too old for this app's
            options, shows a screen this app does not recognise, or is stopped.
        """
        self.workdir.mkdir(parents=True, exist_ok=True)
        with self._lifecycle_lock:
            if self._stopping.is_set():
                raise DictationError(STOPPED_HELP)
            self.handoff_dir = Path(tempfile.mkdtemp(prefix=HANDOFF_DIR_PREFIX, dir=runtime_dir()))
            hook = self._write_hook()
            args = build_claude_args(self.claude_path, self.language)
            env = build_claude_env(dict(os.environ), hook, self.handoff_dir)
            window_size = struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0)
            pid, fd = pty.fork()
            if pid == 0:  # child: becomes Claude Code; it must never return into this program
                try:
                    fcntl.ioctl(0, termios.TIOCSWINSZ, window_size)
                    os.chdir(self.workdir)
                    os.execve(args[0], args, env)
                finally:
                    os._exit(127)
            os.set_blocking(fd, False)
            self.pid, self.fd, self.rows = pid, fd, ()
            self._reader = threading.Thread(
                target=self._drain_output, args=(fd,), daemon=True, name="claude-pty"
            )
            self._reader.start()
        log.info("started hidden Claude Code %s (pid %s)", self.claude_path, pid)
        try:
            self._wait_until_ready()
        except BaseException:
            self.stop()
            raise

    def _wait_until_ready(self) -> None:
        trusted = False
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._stopping.is_set():
                raise DictationError(STOPPED_HELP)
            rows = self.rows
            if not trusted and trust_dialog_choice(rows):
                self._answer_trust_dialog()
                trusted = True
                log.info("trusted the dictation folder %s", self.workdir)
                continue
            screen = "\n".join(rows)
            if shows(screen, LOGGED_OUT_MARKER):
                raise DictationError(LOGGED_OUT_HELP)
            if prompt_ready(rows) and self._screen_quiet_for(STARTUP_QUIET_S):
                return
            if not self.is_alive():
                if shows(screen, UNKNOWN_OPTION_MARKER):
                    raise DictationError(OLD_VERSION_HELP)
                raise DictationError("Claude Code exited at startup. Run `claude` to see why.")
            time.sleep(POLL_S)
        raise DictationError(UNKNOWN_SCREEN_HELP)

    def _answer_trust_dialog(self) -> None:
        """First run only: Claude Code asks whether to trust the app's own empty dictation folder.

        The session has no tools, so "yes" is safe. The arrow starts on "No", so it
        moves first, and Enter is sent only after reading that it is on "Yes".
        """
        if trust_dialog_choice(self.rows) != "yes":
            self._write_key(ARROW_DOWN)
            deadline = time.monotonic() + TRUST_KEY_TIMEOUT_S
            while trust_dialog_choice(self.rows) != "yes":
                if time.monotonic() > deadline:
                    raise DictationError(UNKNOWN_SCREEN_HELP)
                time.sleep(POLL_S)
        self._write_key(ENTER)
        deadline = time.monotonic() + TRUST_KEY_TIMEOUT_S
        while trust_dialog_choice(self.rows):
            if time.monotonic() > deadline:
                raise DictationError(UNKNOWN_SCREEN_HELP)
            time.sleep(POLL_S)

    def _write_hook(self) -> Path:
        """Create the hand-off folder and hook, also if a temp cleaner removed them."""
        self.handoff_dir.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        hook = self.handoff_dir / HOOK_FILE_NAME
        if not os.access(hook, os.X_OK):
            write_private_file(hook, EDITOR_HOOK_SCRIPT)
            hook.chmod(PRIVATE_DIR_MODE)
        return hook

    def _drain_output(self, fd: int) -> None:
        """Before: raw pty bytes, split anywhere. After: ``rows``, the screen they draw."""
        screen = TerminalScreen(PTY_ROWS, PTY_COLS)
        try:
            while not self._reader_stop.is_set():
                ready, _, _ = select.select([fd], [], [], READER_POLL_S)
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    return
                self._observe(screen.feed(chunk))
        except OSError:  # EIO once the child exits
            pass
        finally:
            with self._lifecycle_lock:
                self.fd = None
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.is_alive()

    def _observe(self, rows: tuple[str, ...]) -> None:
        now = time.monotonic()
        phase = voice_phase(rows)
        status = status_text(rows)
        on_screen = {marker for marker in VOICE_ERROR_MARKERS if shows(status, marker)}
        with self._observe_lock:
            if phase:
                self._phases_seen.add(phase)
                self._phase_left_at = 0.0
            elif not self._phase_left_at:
                self._phase_left_at = now
            # An old notice counts again only after it has left the screen.
            self._ignored_failures &= on_screen
            for marker in on_screen - self._ignored_failures:
                if marker not in self._failures:
                    self._failures.append(marker)
            if rows != self.rows:
                self.last_output_at = now
            self.rows = rows

    def _screen_quiet_for(self, seconds: float) -> bool:
        return time.monotonic() - self.last_output_at >= seconds

    def _write(self, data: bytes) -> bool:
        """Write to Claude Code; False if the pty is full, because it has stopped reading.

        Raises
        ------
        DictationError
            If the session has ended.
        """
        with self._lifecycle_lock:
            if self.fd is None or self.pid is None:
                raise DictationError(STOPPED_HELP)
            try:
                return os.write(self.fd, data) == len(data)
            except BlockingIOError:
                return False
            except OSError as error:
                raise DictationError(STOPPED_HELP) from error

    def _write_key(self, key: bytes) -> None:
        """Send one control key, waiting briefly for room.

        Raises
        ------
        DictationError
            If there is none: this session is stuck, so it is stopped, and the next
            press starts a fresh one.
        """
        deadline = time.monotonic() + WRITE_TIMEOUT_S
        while not self._write(key):
            if time.monotonic() > deadline:
                self.stop()
                raise DictationError("Claude Code stopped accepting keys.")
            time.sleep(POLL_S)

    def ready_for_recording(self) -> bool:
        """True if the prompt box is on the screen, so a Space goes into it and nowhere else."""
        return prompt_ready(self.rows)

    def wait_ready_for_recording(self) -> bool:
        """``ready_for_recording``, allowing a moment for a redraw that is under way.

        Answers at once in the usual case, so it adds nothing to the first Space.
        """
        deadline = time.monotonic() + READY_GRACE_S
        while not self.ready_for_recording():
            if time.monotonic() > deadline:
                return False
            time.sleep(POLL_S)
        return True

    def begin_hold(self) -> None:
        """Start a new recording's records: earlier notices and states no longer count."""
        with self._observe_lock:
            self._phases_seen = set()
            self._phase_left_at = 0.0
            self._failures = []
            status = status_text(self.rows)
            self._ignored_failures = {m for m in VOICE_ERROR_MARKERS if shows(status, m)}

    def press_space(self) -> None:
        """Send one Space; called every ``KEY_REPEAT_INTERVAL_S`` while recording.

        A full pty means Claude Code has frozen; ``collect_transcript`` reports that,
        so the caller can keep looping and stay responsive to a release or a cancel.
        """
        self._write(SPACE)

    def cancel_recording(self) -> None:
        """Press Esc: Claude Code stops the microphone and discards this recording's text.

        If voice is not idle again soon, the session is stopped, so nothing can leak
        into the next recording.
        """
        try:
            self._write_key(ESCAPE)
        except DictationError:
            self.stop()  # the text is being discarded anyway
            return
        deadline = time.monotonic() + CANCEL_TIMEOUT_S
        while voice_phase(self.rows):
            if time.monotonic() > deadline:
                log.warning("Claude Code did not cancel the recording; restarting it")
                self.stop()
                return
            time.sleep(POLL_S)

    def collect_transcript(self) -> str:
        """Wait for the final transcript, pull it out with Ctrl+G, and empty the prompt box.

        Returns
        -------
        str
            The dictated text, surrounding whitespace removed ("" if nothing was heard).

        Raises
        ------
        PartialTranscript
            If text exists but may be incomplete: the transcript did not finish in
            time, or Claude Code showed an error notice. Offer it; do not type it.
        DictationError
            If the hook never ran, recording never started, or Claude Code showed
            one of ``VOICE_ERROR_MARKERS`` and there is no text.
        """
        finished = self._wait_for_final_text()
        try:
            raw_text = self._pull_prompt_text()
        except DictationError:
            if not finished:
                self.stop()  # text may still arrive; keep it out of the next recording
            raise
        text = raw_text.strip()
        if not finished:
            # Text may still arrive; a new session keeps it out of the next recording.
            self.stop()
            if text:
                raise PartialTranscript(UNFINISHED_HELP, text)
            raise DictationError(UNFINISHED_HELP)
        with self._observe_lock:
            failure = next(iter(self._failures), None)
        if not text and raw_text.count(" ") > MAX_WARMUP_SPACES:
            raise DictationError(NO_VOICE_HELP)
        if failure:
            message = (
                LOGGED_OUT_HELP
                if failure == LOGGED_OUT_MARKER
                else f"Claude Code reported '{failure}'."
            )
            if text:  # the box is empty by now, so this is the only copy
                raise PartialTranscript(message, text)
            raise DictationError(message)
        return text

    def _wait_for_final_text(self) -> bool:
        """Wait for the footer to say the transcript is final; False if it never does.

        Verified signal (see terminal_screen.py): "Voice: processing…" appeared and
        has gone. If it never appeared, as after a press too short to record, the
        screen must have stopped changing with no voice state showing. That rule
        cannot prove the text is final; it is only reached when the signal is absent.
        """
        released_at = time.monotonic()
        while time.monotonic() - released_at < FINALIZE_TIMEOUT_S:
            if self._stopping.is_set() or not self.is_alive():
                raise DictationError(STOPPED_HELP)
            phase = voice_phase(self.rows)
            if phase == WARMUP and time.monotonic() - released_at > WARMUP_FREEZE_TIMEOUT_S:
                self.stop()  # it ignores every key now; the next press starts a fresh one
                raise DictationError(FROZEN_AT_MICROPHONE_HELP)
            if phase is None:
                with self._observe_lock:
                    processed = PROCESSING in self._phases_seen
                    left_at = self._phase_left_at
                if processed and left_at and time.monotonic() - left_at >= FINAL_SETTLE_S:
                    return True
                if not processed and self._screen_quiet_for(SCREEN_QUIET_S):
                    if LISTENING in self._phases_seen:
                        log.info("finished without seeing the processing state")
                    return True
            time.sleep(POLL_S)
        return False

    def _pull_prompt_text(self) -> str:
        """Run the Ctrl+G hand-off for one new generation; returns the text as written.

        A problem before Ctrl+G is sent leaves the text in the prompt box, where the
        next hand-off picks it up, so the session keeps running. After Ctrl+G, a
        session that does not answer is stuck and is stopped.
        """
        self._generation += 1
        generation = self._generation
        folder = self.handoff_dir
        if folder is None or self._stopping.is_set():
            raise DictationError(STOPPED_HELP)
        try:
            self._write_hook()
            write_private_file(folder / "generation", str(generation))
        except OSError as error:
            raise DictationError(
                "Could not prepare the hand-off folder; the text stays in the hidden "
                "session and comes with your next dictation."
            ) from error
        result = folder / f"result.{generation}"
        partial = folder / f"result.{generation}.partial"
        done = folder / f"done.{generation}"
        visible_text = prompt_box_text(self.rows)  # the screen changes once the editor opens
        try:
            self._write_key(CTRL_G)
        except DictationError:
            return self._recover_handoff(result, partial, visible_text)
        deadline = time.monotonic() + EDITOR_TIMEOUT_S
        while not done.exists():
            if self._stopping.is_set():
                raise DictationError(STOPPED_HELP)
            if time.monotonic() > deadline:
                return self._recover_handoff(result, partial, visible_text)
            time.sleep(POLL_S)
        text = self._read_handoff(result)
        deadline = time.monotonic() + POST_EDITOR_TIMEOUT_S
        while not self.ready_for_recording():
            if time.monotonic() > deadline:
                log.warning("the prompt box did not come back after the hand-off; restarting")
                self.stop()
                break
            time.sleep(POLL_S)
        for leftover in (result, done):
            try:
                leftover.unlink(missing_ok=True)
            except OSError as error:
                log.warning("could not remove a hand-off file: %s", error.strerror)
        return text

    def _recover_handoff(self, result: Path, partial: Path, visible_text: str) -> str:
        """The hook did not finish. Save the best copy there is, then replace the stuck session.

        Stopping the session destroys its prompt box, so the text must be out first:
        the published copy if there is one (complete, so it is returned), else the
        partial copy or the box as last seen on the screen (possibly incomplete, so
        they are raised as ``PartialTranscript``).
        """
        frozen_at_microphone = voice_phase(self.rows) == WARMUP
        try:
            if result.exists():
                return self._read_handoff(result)
            copied = self._read_handoff(partial).strip() if partial.exists() else ""
            if copied or visible_text:
                raise PartialTranscript(HOOK_FAILED_HELP, copied or visible_text)
        finally:
            self.stop()
        if frozen_at_microphone:
            raise DictationError(FROZEN_AT_MICROPHONE_HELP)
        raise DictationError(HOOK_FAILED_HELP)

    @staticmethod
    def _read_handoff(path: Path) -> str:
        try:
            return path.read_bytes().decode("utf-8", "replace")
        except OSError as error:
            raise DictationError("Could not read the handed-over text.") from error

    def stop(self) -> None:
        """Stop Claude Code and its helpers, and remove the hand-off folder with any transcript.

        Only this session's own process group is signalled. The reader keeps draining
        until the process has exited, because on macOS it cannot finish exiting while
        unread output is waiting. Safe from any thread and safe to repeat.
        """
        self._stopping.set()
        with self._lifecycle_lock:
            pid = self.pid
        if pid is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except OSError:  # gone (macOS says EPERM once only a zombie is left)
                    pass
                if self._wait_for_exit(STOP_GRACE_S):
                    break
            log.info("stopped hidden Claude Code (pid %s)", pid)
        self._reader_stop.set()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(READER_JOIN_TIMEOUT_S)
        if pid is not None:
            self._wait_for_exit(STOP_GRACE_S)  # the pty is closed now, which frees a stuck exit
        with self._lifecycle_lock:
            if self.pid is not None:
                log.warning("hidden Claude Code (pid %s) did not exit", self.pid)
                self.pid = None
            folder, self.handoff_dir = self.handoff_dir, None
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)

    def _wait_for_exit(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while self.is_alive():
            if time.monotonic() > deadline:
                return False
            time.sleep(POLL_S)
        return True
