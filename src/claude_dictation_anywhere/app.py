"""Start Claude Dictation Anywhere: tray icon, shortcut listener, hidden Claude Code, settings page.

Usage
-----
claude-dictation-anywhere              # tray app (uv run claude-dictation-anywhere from the repo)
claude-dictation-anywhere --open       # also open the settings page in your browser
claude-dictation-anywhere --test 5     # record 5 s, print the text, nothing typed

Threads: on Linux the tray icon runs its GTK loop in a background thread and the
main thread waits, so Ctrl+C works; on macOS AppKit requires the tray on the
main thread.
"""

import argparse
import fcntl
import logging
import signal
import sys
import threading
import time
from datetime import date

from .claude_session import (
    KEY_REPEAT_INTERVAL_S,
    ClaudeVoiceSession,
    DictationError,
    find_claude,
)
from .controller import DictationController
from .settings import IS_MAC, data_dir, load_settings, runtime_dir, settings_path
from .tray import TrayIcon
from .web_ui import SettingsPage

log = logging.getLogger("claude_dictation_anywhere")


def next_log_path():
    """logs/YYYY-MM-DD_NNN.log with the next free sequence number for today."""
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    taken = [int(p.stem.rsplit("_", 1)[1]) for p in log_dir.glob(f"{today}_*.log")]
    return log_dir / f"{today}_{max(taken, default=0) + 1:03d}.log"


def hold_single_instance_lock():
    """Exit if another copy already runs; two would fight over the shortcut."""
    handle = open(runtime_dir() / "app.lock", "w")  # noqa: SIM115 - kept open to hold the lock
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
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
    except DictationError as error:
        sys.exit(str(error))
    finally:
        session.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code's /voice dictation in every app.")
    parser.add_argument("--open", action="store_true", help="open the settings page at start")
    parser.add_argument(
        "--test", type=float, metavar="SECONDS", help="record this long, print the text"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(next_log_path()), logging.StreamHandler()],
    )
    lock = hold_single_instance_lock()  # noqa: F841 - held until exit
    if args.test:
        run_test(args.test)
        return

    first_run = not settings_path().exists()
    controller = DictationController(load_settings())
    page = SettingsPage(controller)
    page.start()
    stopped = threading.Event()
    tray = TrayIcon(controller, open_settings=page.open, quit_app=stopped.set)
    controller.start()
    if args.open or first_run:
        page.open()

    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    try:
        if IS_MAC:
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
        tray.stop()
        controller.shutdown()
        page.stop()
        log.info("quit")


if __name__ == "__main__":
    main()
