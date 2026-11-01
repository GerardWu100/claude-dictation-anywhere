"""Start Claude Dictation Anywhere: tray icon, shortcut listener, hidden Claude Code, settings page.

Usage
-----
claude-dictation-anywhere              # tray app (uv run claude-dictation-anywhere from the repo)
claude-dictation-anywhere --open       # also open the settings page in your browser
claude-dictation-anywhere --test 5     # record 5 s, print the text, nothing typed
claude-dictation-anywhere --install-launcher    # Linux: show it in the desktop's app search

Threads: on Linux the tray icon runs its GTK loop in a background thread and the
main thread waits, so Ctrl+C works; on macOS AppKit requires the tray on the
main thread.

The tray and the input listener load only when the tray app actually runs, so
--help, --test, and --install-launcher work without a desktop session.
"""

import argparse
import fcntl
import logging
import math
import os
import re
import signal
import sys
import threading
import time
from datetime import date

from .claude_session import (
    KEY_REPEAT_INTERVAL_S,
    ClaudeVoiceSession,
    DictationError,
    PartialTranscript,
    find_claude,
    remove_stale_handoffs,
)
from .launcher import install_launcher, uninstall_launcher
from .settings import (
    IS_MAC,
    PRIVATE_FILE_MODE,
    UnsafeFolderError,
    config_dir,
    data_dir,
    load_settings,
    make_private_dir,
    read_settings,
    runtime_dir,
    settings_path,
    tighten_private_file,
)
from .web_ui import SettingsPage, open_in_browser, running_page_url_path

log = logging.getLogger("claude_dictation_anywhere")

LOG_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d+)\.log")
MAX_LOG_FILES = 30
# Claude Code ends a recording at two minutes; --test stops just short of that.
MAX_TEST_SECONDS = 115


def next_log_path():
    """logs/YYYY-MM-DD_NNN.log with the next free sequence number for today.

    Files that do not follow the pattern are ignored. Only the newest
    ``MAX_LOG_FILES`` logs are kept.
    """
    log_dir = make_private_dir(data_dir() / "logs")
    logs = sorted(
        (match.groups(), path)
        for path in log_dir.iterdir()
        if (match := LOG_NAME.fullmatch(path.name))
    )
    # Sorted by (date, sequence as text); widths are fixed until 999 logs a day.
    for _, old_log in logs[: max(0, len(logs) - MAX_LOG_FILES + 1)]:
        old_log.unlink(missing_ok=True)
    today = date.today().isoformat()
    taken = [int(sequence) for (day, sequence), _ in logs if day == today]
    return log_dir / f"{today}_{max(taken, default=0) + 1:03d}.log"


def open_private_log(path) -> logging.FileHandler:
    """A log handler whose file only this user can read."""
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE))
    return logging.FileHandler(path)


def tighten_existing_files() -> None:
    """Make files written by earlier versions private; this app's own folders only."""
    make_private_dir(config_dir())
    make_private_dir(data_dir())
    tighten_private_file(settings_path())
    tighten_private_file(data_dir() / "history.jsonl")
    logs = data_dir() / "logs"
    if logs.is_dir():
        for path in logs.iterdir():
            if LOG_NAME.fullmatch(path.name):
                tighten_private_file(path)


def test_duration(value: str) -> float:
    """--test value: finite, above zero, and within what one recording can last."""
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_TEST_SECONDS:
        raise argparse.ArgumentTypeError(f"must be above 0 and at most {MAX_TEST_SECONDS} seconds")
    return seconds


def hold_single_instance_lock(open_running_page: bool):
    """Exit if another copy already runs; two would fight over the shortcut.

    With ``open_running_page`` (the app launcher passes --open), first open the
    running copy's settings page, so clicking the launcher always shows something.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    handle = os.open(runtime_dir() / "app.lock", flags, PRIVATE_FILE_MODE)  # kept open: the lock
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        url_path = running_page_url_path()
        if open_running_page and url_path.exists():
            open_in_browser(url_path.read_text(encoding="utf-8"))
        sys.exit("Claude Dictation Anywhere is already running (look for its tray icon).")
    return handle


def run_test(seconds: float) -> None:
    """Record for ``seconds`` without any shortcut, then print the transcript."""
    settings = load_settings()
    claude_path = find_claude(settings["claude_path"])
    if not claude_path:
        sys.exit("Claude Code was not found on this system.")
    session = ClaudeVoiceSession(claude_path, settings["language"])
    print(f"Starting hidden Claude Code ({claude_path})...", flush=True)
    try:
        session.start()
        print(f"Speak now for {seconds:g} seconds.", flush=True)
        session.begin_hold()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            session.press_space()
            time.sleep(KEY_REPEAT_INTERVAL_S)
        started = time.monotonic()
        text = session.collect_transcript()
        print(f"Transcript after {time.monotonic() - started:.1f} s: {text!r}")
    except PartialTranscript as error:
        print(f"Incomplete transcript: {error.text!r}")
        sys.exit(str(error))
    except DictationError as error:
        sys.exit(str(error))
    finally:
        session.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code's /voice dictation in every app.")
    parser.add_argument("--open", action="store_true", help="open the settings page at start")
    parser.add_argument(
        "--test", type=test_duration, metavar="SECONDS", help="record this long, print the text"
    )
    if not IS_MAC:
        parser.add_argument(
            "--install-launcher", action="store_true", help="show the app in the app search"
        )
        parser.add_argument(
            "--uninstall-launcher", action="store_true", help="remove it from the app search"
        )
    args = parser.parse_args()
    if getattr(args, "install_launcher", False):
        install_launcher()
        return
    if getattr(args, "uninstall_launcher", False):
        uninstall_launcher()
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[open_private_log(next_log_path()), logging.StreamHandler()],
    )
    try:
        lock = hold_single_instance_lock(args.open)  # noqa: F841 - held until exit
    except UnsafeFolderError as error:
        sys.exit(str(error))
    tighten_existing_files()
    remove_stale_handoffs()
    if args.test is not None:
        run_test(args.test)
        return
    run_app(args.open)


def run_app(open_page: bool) -> None:
    """The tray app; returns when it quits."""
    from .controller import DictationController
    from .tray import TrayIcon

    first_run = not settings_path().exists()
    settings, settings_problems = read_settings()
    controller = DictationController(settings, settings_problems)
    page = SettingsPage(controller)
    page.start()
    stopped = threading.Event()
    tray = TrayIcon(controller, open_settings=page.open, quit_app=stopped.set)
    controller.start()
    if open_page or first_run:
        page.open()

    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    try:
        if IS_MAC:
            from PyObjCTools import MachSignals

            # Inside AppKit's loop the main thread runs no Python, so plain signal
            # handlers never fire; Mach signals arrive as run-loop events instead.
            for signum in (signal.SIGINT, signal.SIGTERM):
                MachSignals.signal(signum, lambda _signum: stopped.set())
            threading.Thread(target=lambda: (stopped.wait(), tray.stop()), daemon=True).start()
            tray.run()
        else:
            threading.Thread(target=tray.run, daemon=True, name="tray").start()
            while not stopped.wait(timeout=0.5):
                pass
    except KeyboardInterrupt:
        pass
    finally:
        # A second Ctrl+C while the hidden session shuts down would leave it running.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for cleanup in (tray.stop, controller.shutdown, page.stop):
            try:  # one failure must not skip the rest, above all the speakers and Claude Code
                cleanup()
            except Exception:
                log.exception("cleanup failed")
        log.info("quit")


if __name__ == "__main__":
    main()
