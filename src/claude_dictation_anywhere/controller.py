"""The dictation state machine: shortcut presses in, transcripts out.

    idle ──press──► starting (only if the hidden Claude Code is not running)
      ▲                │
      │                ▼
      │            listening ── hold mode: release ──┐
      │                │        toggle mode: press ──┤
      │                ▼                             ▼
      └─────────── transcribing ──► copy / type the text, add to history

The shortcut listener only queues "down" / "up" events, so input keeps
flowing while a transcript is pending; one worker thread does the rest.
"""

import logging
import queue
import threading
import time

from . import feedback, hotkeys, output
from .claude_session import (
    KEY_REPEAT_INTERVAL_S,
    ClaudeVoiceSession,
    DictationError,
    claude_version,
    find_claude,
)
from .history import History
from .settings import check_settings, data_dir, save_settings

# Claude Code ends a recording at two minutes; stop just before so the text is kept.
MAX_RECORDING_S = 115
IDLE_CHECK_INTERVAL_S = 30
CAPTURE_TIMEOUT_S = 15
HISTORY_PAGE_SIZE = 100

# Settings whose change needs a restart of the shortcut listener / hidden session.
LISTENER_SETTINGS = {"shortcut_key", "shortcut_device", "grab"}
SESSION_SETTINGS = {"language", "claude_path"}

log = logging.getLogger(__name__)


class DictationController:
    """Owns the settings, the hidden Claude Code session, the listener, and the history.

    ``state`` is one of: "idle", "starting", "listening", "transcribing",
    "paused", "capturing". Callbacks in ``on_change`` run after every change
    (the tray icon uses this).
    """

    def __init__(self, settings: dict):
        self.settings = settings
        self.state = "idle"
        self.last_error = ""
        self.last_text = ""
        self.capture = {"status": "none"}  # status: none | waiting | done | timeout | rejected
        self.on_change: list = []
        self.history = History(data_dir() / "history.jsonl")
        self.events: queue.Queue[str] = queue.Queue()
        self.banner = feedback.Banner()
        self.listener: hotkeys.TriggerListener | None = None
        self.session: ClaudeVoiceSession | None = None
        self.claude_path = ""
        self.claude_version = ""
        self.last_used = time.monotonic()
        self._lock = threading.RLock()
        self._locate_claude()

    # ---- public: called by the app, the tray, and the settings page ----

    def start(self) -> None:
        self._start_listener()
        threading.Thread(target=self._worker, daemon=True, name="dictation").start()
        if self.settings["prewarm"]:
            self.events.put("prewarm")

    def shutdown(self) -> None:
        if self.listener:
            self.listener.stop()
        if self.session:
            self.session.stop()
        self.banner.close()

    def set_paused(self, paused: bool) -> None:
        self._set_state("paused" if paused else "idle")

    def update_settings(self, update: dict) -> dict:
        """Check, save, and apply ``update``; returns the full settings. Raises ValueError."""
        clean = check_settings(update)
        with self._lock:
            changed = {k for k, v in clean.items() if self.settings.get(k) != v}
            self.settings.update(clean)
            save_settings(self.settings)
        if changed & SESSION_SETTINGS:
            self._locate_claude()
            self.events.put("restart-session")
        if changed & LISTENER_SETTINGS:
            self._start_listener()
        self._notify()
        return self.settings

    def capture_shortcut(self) -> None:
        """Wait (in the background) for the next key or button press and make it the shortcut."""
        if self.state in ("capturing", "listening", "transcribing"):
            return
        self.capture = {"status": "waiting"}
        self._set_state("capturing")
        threading.Thread(target=self._capture, daemon=True, name="capture").start()

    def status(self) -> dict:
        """Everything the settings page shows."""
        return {
            "state": self.state,
            "last_error": self.last_error,
            "claude": {"path": self.claude_path, "version": self.claude_version},
            "session_running": bool(self.session and self.session.is_alive()),
            "shortcut": hotkeys.describe_shortcut(
                self.settings["shortcut_key"], self.settings["shortcut_device"]
            ),
            "capture": self.capture,
            "warnings": output.missing_tools(self.settings),
            "settings": self.settings,
            "history": self.history.recent(HISTORY_PAGE_SIZE) if self.settings["history"] else [],
        }

    # ---- shortcut events (from the listener thread) ----

    def _on_down(self) -> None:
        if self.state not in ("paused", "capturing"):
            self.events.put("down")

    def _on_up(self) -> None:
        self.events.put("up")

    # ---- internals ----

    def _locate_claude(self) -> None:
        self.claude_path = find_claude(self.settings["claude_path"]) or ""
        self.claude_version = claude_version(self.claude_path) if self.claude_path else ""
        if not self.claude_path:
            self.last_error = (
                "Claude Code was not found. Install it (https://claude.com/claude-code) "
                "or set its path in settings."
            )

    def _start_listener(self) -> None:
        if self.listener:
            self.listener.stop()
            self.listener = None
        listener = hotkeys.TriggerListener(self.settings, self._on_down, self._on_up)
        try:
            listener.start()
            self.listener = listener
        except (ValueError, OSError) as error:
            self.last_error = f"Shortcut not active: {error}"
            log.warning(self.last_error)

    def _capture(self) -> None:
        if self.listener:
            self.listener.stop()
            self.listener = None
        found = hotkeys.capture_next_input(CAPTURE_TIMEOUT_S)
        if found is None:
            self.capture = {"status": "timeout"}
        elif hotkeys.is_printable_key(found[0]):
            self.capture = {
                "status": "rejected",
                "message": "That key types text. Pick a function key, a modifier, or a mouse button.",
            }
        else:
            key, device = found
            self.settings.update(shortcut_key=key, shortcut_device=device)
            save_settings(self.settings)
            self.capture = {
                "status": "done",
                "shortcut": hotkeys.describe_shortcut(key, device),
            }
            log.info("new shortcut %s", self.capture["shortcut"])
        self._start_listener()
        self._set_state("idle")

    def _set_state(self, state: str) -> None:
        self.state = state
        self._notify()

    def _notify(self) -> None:
        for callback in self.on_change:
            try:
                callback(self)
            except Exception:  # a broken indicator must never stop dictation
                log.exception("state callback failed")

    def _ensure_session(self) -> None:
        if self.session and self.session.is_alive():
            return
        if not self.claude_path:
            raise DictationError(self.last_error or "Claude Code was not found.")
        self._set_state("starting")
        self._banner("Starting Claude Code…")
        self.session = ClaudeVoiceSession(self.claude_path, self.settings["language"])
        self.session.start()

    def _worker(self) -> None:
        while True:
            try:
                event = self.events.get(timeout=IDLE_CHECK_INTERVAL_S)
            except queue.Empty:
                self._stop_if_idle()
                continue
            try:
                if event == "prewarm":
                    self._ensure_session()
                    self.last_error = ""
                elif event == "restart-session" and self.session:
                    self.session.stop()
                elif event == "down" and self.state not in ("paused", "capturing"):
                    self._dictate_once()
            except DictationError as error:
                self.last_error = str(error)
                log.warning(self.last_error)
                self._banner(f"⚠ {error}")
                time.sleep(3)
            finally:
                self.banner.close()
                if self.state not in ("paused", "capturing"):
                    self._set_state("idle")

    def _stop_if_idle(self) -> None:
        idle_minutes = self.settings["idle_shutdown_minutes"]
        idle_s = time.monotonic() - self.last_used
        if idle_minutes and idle_s > idle_minutes * 60 and self.session and self.session.is_alive():
            log.info("idle for %.0f min, stopping the hidden Claude Code", idle_s / 60)
            self.session.stop()
            self._notify()

    def _dictate_once(self) -> None:
        self.last_used = time.monotonic()
        self._ensure_session()
        toggle = self.settings["mode"] == "toggle"
        self._set_state("listening")
        self._banner(
            "🎙 Listening… " + ("(press again to finish)" if toggle else "(release to finish)")
        )
        if self.settings["sounds"]:
            feedback.play_sound("start")
        started = time.monotonic()
        self.session.begin_hold()
        # Before: shortcut down, prompt box empty. Hold Space until the stop signal:
        # hold mode stops on "up"; toggle mode ignores "up" and stops on the next "down".
        while time.monotonic() - started < MAX_RECORDING_S:
            self.session.press_space()
            try:
                event = self.events.get(timeout=KEY_REPEAT_INTERVAL_S)
            except queue.Empty:
                continue
            if (event == "up" and not toggle) or (event == "down" and toggle):
                break
        # After: Claude Code has the recording; fetch the finished transcript.
        seconds = time.monotonic() - started
        self._set_state("transcribing")
        self._banner("✍ Transcribing…")
        if self.settings["sounds"]:
            feedback.play_sound("stop")
        text = self.session.collect_transcript()
        log.info("recorded %.1f s, got %d characters", seconds, len(text))
        self.last_used = time.monotonic()
        if not text:
            return
        self.last_text = text
        self.last_error = ""
        if self.settings["history"]:
            self.history.add(text, seconds)
        try:
            output.deliver(text, self.settings)
        except Exception as error:
            raise DictationError(f"Got the text but could not insert it: {error}") from error

    def _banner(self, title: str) -> None:
        if self.settings["notifications"]:
            self.banner.show(title)
