"""Drive a hidden copy of the Claude Code installed on this system, only for its /voice dictation.

Claude Code runs in a pseudo-terminal (pty: a fake terminal a program can
drive), and this module works it the way a person would:

    hold    -> write " " every 30 ms; Claude Code reads fast-repeating spaces as
               a held Space bar and records the microphone (warm-up ~0.13 s)
    release -> stop the spaces; Claude Code puts the transcript in its prompt
               box, unsent
    collect -> press Ctrl+G ("edit prompt in external editor"); the editor is a
               tiny hook script that hands the text over and empties the box

No prompt is ever sent to the model, so no tokens are used, and the Claude.ai
login never leaves Claude Code. The app does not bundle Claude Code: it finds
the one already installed (see ``find_claude``).
"""

import fcntl
import json
import logging
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path

from .settings import data_dir, runtime_dir

# A small fake screen gives Claude Code less to redraw.
PTY_ROWS, PTY_COLS = 24, 100
# Terminals repeat a held key about every 30 ms; Claude Code detects "held"
# from that rhythm, so the fake Space must repeat at least this fast.
KEY_REPEAT_INTERVAL_S = 0.03
STARTUP_TIMEOUT_S = 25
# After release, the transcript counts as final once the hidden screen has
# stopped changing for this long.
SCREEN_QUIET_S = 0.6
FINALIZE_TIMEOUT_S = 12
EDITOR_TIMEOUT_S = 3
SCREEN_BUFFER_CHARS = 20_000  # rolling tail of visible text kept for checks
STOP_GRACE_S = 0.5
CTRL_G = b"\x07"
SPACE = b" "
ARROW_DOWN_THEN_ENTER = (b"\x1b[B", b"\r")
# Model for the hidden session. Dictation never sends a prompt; the cheapest
# model only limits the cost if something ever did.
HIDDEN_SESSION_MODEL = "haiku"

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

# Text on the hidden screen that tells the session state apart.
READY_MARKER = "❯"  # the prompt arrow, drawn once the input box is ready
TRUST_MARKER = "trust this folder"
LOGGED_OUT_MARKER = "Not logged in"
# Messages Claude Code shows when dictation fails (from its voice docs).
VOICE_ERROR_MARKERS = (
    LOGGED_OUT_MARKER,
    "Voice mode requires",
    "Voice mode is disabled",
    "Microphone access is denied",
    "Voice input is failing",
    "No audio detected",
    "Voice connection failed",
    "Voice stream error",
    "Dictation error",
)
# Claude Code types the first couple of repeated spaces before it notices the
# hold and deletes them; more left over than this means it never recorded.
MAX_WARMUP_SPACES = 5
LOGGED_OUT_HELP = (
    "Claude Code is not logged in. Run `claude` in a terminal, type /login, "
    "and sign in with your Claude.ai account."
)
NO_VOICE_HELP = (
    "Recording never started (Claude Code typed the spaces instead). "
    "Usually it is not logged in: run `claude`, type /login, then try /voice once."
)

# Flags for the hidden session, all standard Claude Code options. Safe mode
# skips your CLAUDE.md, hooks, plugins, skills, and MCP servers, so none of your
# own setup runs in it. No tools: even a prompt sent by mistake could not touch
# files or run commands.
CLAUDE_BASE_FLAGS = ["--safe-mode", "--tools", ""]

# Stand-in "text editor" Claude Code runs on Ctrl+G. It copies the prompt file
# (its last argument) to the hand-off path and empties it, clearing the box.
EDITOR_HOOK_SCRIPT = """#!/bin/sh
for prompt_file in "$@"; do :; done
cp "$prompt_file" "$CDA_RESULT.partial"
mv "$CDA_RESULT.partial" "$CDA_RESULT"
: > "$prompt_file"
"""

ANSI_ESCAPE = re.compile(
    r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]"  # CSI: cursor moves, colours, modes
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: window title, hyperlinks
    r"|\x1b[()][A-Za-z0-9]"  # character-set selection
    r"|\x1b[78=>]"  # save/restore cursor, keypad modes
)

log = logging.getLogger(__name__)


class DictationError(RuntimeError):
    """A problem the user must fix or retry (not logged in, no microphone, ...)."""


def strip_ansi(raw: str) -> str:
    """Return terminal output with escape sequences removed, leaving the visible text."""
    return ANSI_ESCAPE.sub("", raw)


def screen_shows(screen: str, marker: str) -> bool:
    """True if ``marker`` is on the hidden screen.

    Claude Code often draws gaps between words as cursor moves, not spaces, so
    once escapes are stripped "trust this folder" can read "trustthisfolder".
    Compare with all whitespace removed.
    """
    return re.sub(r"\s", "", marker) in re.sub(r"\s", "", screen)


def find_claude(configured_path: str = "") -> str | None:
    """Path of the Claude Code program installed on this system, or None.

    Order: the path set in settings, then `claude` on PATH, then the usual
    install locations in ``CLAUDE_CANDIDATE_PATHS``.
    """
    if configured_path:
        path = Path(configured_path).expanduser()
        return str(path) if os.access(path, os.X_OK) else None
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


def build_claude_env(base_env: dict[str, str], hook: Path, result: Path) -> dict[str, str]:
    """Environment for the hidden session.

    Every ``CLAUDE*`` / ``ANTHROPIC*`` variable is dropped: started from inside
    another Claude Code session, those point the child at the parent's login or
    proxy, and an ``ANTHROPIC_API_KEY`` turns dictation off entirely (it needs a
    Claude.ai login).
    """
    env = {k: v for k, v in base_env.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env.update(
        TERM="xterm-256color",
        EDITOR=str(hook),
        VISUAL=str(hook),
        CDA_RESULT=str(result),
        DISABLE_AUTOUPDATER="1",  # never update itself mid-dictation
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
    A reader thread drains the pty all the time; a terminal program blocks once
    nobody reads its output. ``screen`` keeps the last ``SCREEN_BUFFER_CHARS``
    of visible text and ``last_output_at`` when it last changed.
    """

    def __init__(self, claude_path: str, language: str):
        self.claude_path = claude_path
        self.language = language
        self.pid: int | None = None
        self.fd: int | None = None
        self.screen = ""
        self.last_output_at = 0.0
        self._lock = threading.Lock()
        self.workdir = data_dir() / "dictation"  # neutral name: Claude Code uses it as a word hint
        self.hook = runtime_dir() / "editor_hook.sh"
        self.result = runtime_dir() / "transcript.txt"

    def is_alive(self) -> bool:
        if self.pid is None:
            return False
        try:
            finished_pid, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            finished_pid = self.pid
        if finished_pid == self.pid:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
            self.pid = self.fd = None
            return False
        return True

    def start(self) -> None:
        """Start Claude Code in a pty and wait until its prompt box is ready.

        Raises
        ------
        DictationError
            If Claude Code is not logged in, exits, or never shows its prompt
            within ``STARTUP_TIMEOUT_S``.
        """
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.hook.write_text(EDITOR_HOOK_SCRIPT)
        self.hook.chmod(0o700)
        args = build_claude_args(self.claude_path, self.language)
        env = build_claude_env(dict(os.environ), self.hook, self.result)
        pid, fd = pty.fork()
        if pid == 0:  # child: becomes Claude Code
            os.chdir(self.workdir)
            os.execve(args[0], args, env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        self.pid, self.fd, self.screen = pid, fd, ""
        threading.Thread(target=self._drain_output, args=(fd,), daemon=True).start()
        log.info("started hidden Claude Code %s (pid %s)", self.claude_path, pid)

        trusted = False
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            screen = self.screen
            if not trusted and screen_shows(screen, TRUST_MARKER):
                # First run only: Claude Code asks whether to trust the app's own
                # empty dictation folder. The session has no tools, so "yes" is safe.
                time.sleep(SCREEN_QUIET_S)
                for key in ARROW_DOWN_THEN_ENTER:
                    os.write(fd, key)
                    time.sleep(0.3)
                trusted = True
                self.begin_hold()  # forget the trust screen, whose "❯" is not the prompt
                log.info("trusted the dictation folder %s", self.workdir)
                continue
            if screen_shows(screen, LOGGED_OUT_MARKER):
                self.stop()
                raise DictationError(LOGGED_OUT_HELP)
            ready = READY_MARKER in screen and not screen_shows(screen, TRUST_MARKER)
            if ready and self._screen_quiet_for(SCREEN_QUIET_S):
                return
            if not self.is_alive():
                raise DictationError(f"Claude Code exited at startup: {self.screen[-300:]!r}")
            time.sleep(0.1)
        self.stop()
        raise DictationError(f"Claude Code did not become ready: {self.screen[-300:]!r}")

    def _drain_output(self, fd: int) -> None:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:  # EIO once the child exits
                return
            if not chunk:
                return
            text = strip_ansi(chunk.decode("utf-8", "ignore"))
            with self._lock:
                self.screen = (self.screen + text)[-SCREEN_BUFFER_CHARS:]
                self.last_output_at = time.monotonic()

    def _screen_quiet_for(self, seconds: float) -> bool:
        return time.monotonic() - self.last_output_at >= seconds

    def begin_hold(self) -> None:
        """Forget earlier screen text, so error checks after this hold see only its own output."""
        with self._lock:
            self.screen = ""

    def press_space(self) -> None:
        """Send one Space; called every ``KEY_REPEAT_INTERVAL_S`` while recording."""
        os.write(self.fd, SPACE)

    def collect_transcript(self) -> str:
        """Wait for the transcript to settle, pull it out with Ctrl+G, and empty the prompt box.

        Returns
        -------
        str
            The dictated text, surrounding whitespace removed ("" if nothing was heard).

        Raises
        ------
        DictationError
            If the hook never ran, recording never started, or Claude Code showed
            one of ``VOICE_ERROR_MARKERS``.
        """
        deadline = time.monotonic() + FINALIZE_TIMEOUT_S
        while not self._screen_quiet_for(SCREEN_QUIET_S) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.result.unlink(missing_ok=True)
        os.write(self.fd, CTRL_G)
        deadline = time.monotonic() + EDITOR_TIMEOUT_S
        while not self.result.exists():
            if time.monotonic() > deadline:
                raise DictationError(f"Ctrl+G hook never ran: {self.screen[-300:]!r}")
            time.sleep(0.02)
        raw_text = self.result.read_text(encoding="utf-8")
        self.result.unlink(missing_ok=True)
        text = raw_text.strip()
        if not text and raw_text.count(" ") > MAX_WARMUP_SPACES:
            raise DictationError(NO_VOICE_HELP)
        if not text:
            failure = next((m for m in VOICE_ERROR_MARKERS if screen_shows(self.screen, m)), None)
            if failure == LOGGED_OUT_MARKER:
                raise DictationError(LOGGED_OUT_HELP)
            if failure:
                raise DictationError(f"Claude Code reported '{failure}'.")
        return text

    def stop(self) -> None:
        if self.pid is None:
            return
        pid = self.pid
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            time.sleep(STOP_GRACE_S)
            if not self.is_alive():
                break
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.pid = self.fd = None
        log.info("stopped hidden Claude Code (pid %s)", pid)
