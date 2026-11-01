"""The dictation state machine: shortcut presses in, transcripts out.

    idle ──press──► starting (only if the hidden Claude Code is not running)
      ▲                │
      │                ▼
      │            listening ── hold mode: release ──┐
      │                │        toggle mode: press ──┤
      │                ▼                             ▼
      └─────────── transcribing ──► copy / type the text, add to history

One worker thread does all of this and is the only one to change the phase. Other
threads ask: the listener queues events, and pause, quit, and a lost shortcut set
a cancel reason that the worker reads every 30 ms while recording.

What each event does in each phase:

                  idle / starting       listening              transcribing
    press         records, if still     toggle mode: stops     refused whole: its
                  wanted when ready     hold mode: ignored     release is dropped too
    release       ends a press still    hold mode: stops       ignored
                  waiting for startup   toggle mode: ignored
    lost shortcut cancels a waiting     stops; text is kept    no effect
                  press                 aside, not typed
    pause         cancels a waiting     stops; text discarded  text kept aside, typing
                  press                                        stops if it has begun
    quit          stops                 stops; text discarded  stops

Settings changes need no event to survive: each recording uses one snapshot of
the settings, and the session is compared with the current settings between
recordings, so a change made at any moment applies to the next recording.
"""

import logging
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from . import feedback, hotkeys, output, system_audio
from .claude_session import (
    KEY_REPEAT_INTERVAL_S,
    ClaudeVoiceSession,
    DictationError,
    PartialTranscript,
    claude_version,
    find_claude,
)
from .history import History
from .settings import IS_MAC, UnsafeFolderError, check_settings, data_dir, save_settings

if IS_MAC:
    from . import mac_events

# Claude Code ends a recording at two minutes; stop just before so the text is kept.
MAX_RECORDING_S = 115
IDLE_CHECK_INTERVAL_S = 30
CAPTURE_TIMEOUT_S = 15
# Least time between retries of a shortcut that could not start: blocked by macOS
# until you grant Input Monitoring, or its Linux device unplugged. Retries run on
# each settings-page poll and every IDLE_CHECK_INTERVAL_S otherwise.
LISTENER_RETRY_INTERVAL_S = 5
HISTORY_PAGE_SIZE = 100
ERROR_BANNER_S = 3
# Setup warnings ask macOS or look for programs; the page polls faster than they change.
WARNINGS_CACHE_S = 5
MAX_RECOVERY_ENTRIES = 5
# After a stop, the banner, sound, and mute may still be finishing. Their helpers
# have deadlines of their own, and a late one cannot undo the restore (see _mute_while).
ANNOUNCER_JOIN_TIMEOUT_S = 0.5
# Text is not typed while a shortcut key is held: on Linux, Ctrl still down would
# turn typed letters into shortcuts.
RELEASE_WAIT_S = 2
RELEASE_POLL_S = 0.02
# Quitting: the worker gets this long to finish by itself, then the session is stopped
# under it, then it gets this long again.
SHUTDOWN_WORKER_WAIT_S = 0.5
SHUTDOWN_JOIN_TIMEOUT_S = 5
THREAD_JOIN_TIMEOUT_S = 2

# Settings whose change needs a restart of the shortcut listener.
LISTENER_SETTINGS = {"shortcut_key", "shortcut_device", "grab"}
SESSION_SETTINGS = {"language", "claude_path"}

# Worker event kinds.
PRESS, RELEASE, PREWARM, WAKE = "press", "release", "prewarm", "wake"
# Why a recording is being cut short. PAUSED and QUITTING discard its text.
PAUSED, INPUT_LOST, QUITTING = "paused", "input lost", "quitting"
NOT_FOUND_HELP = (
    "Claude Code was not found. Install it (https://claude.com/claude-code) "
    "or set its path in settings."
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    """One item for the worker. ``gesture`` numbers presses, so stale ones can be told apart."""

    kind: str
    gesture: int = 0


class DictationController:
    """Owns the settings, the hidden Claude Code session, the listener, and the history.

    ``state`` is one of: "idle", "starting", "listening", "transcribing", "paused",
    "capturing", "failed". Callbacks in ``on_change`` run after every change (the
    tray icon uses this).

    ``settings`` is replaced as a whole, never edited in place, so a reader always
    holds one consistent snapshot. Disk, memory, and the running parts agree: a new
    snapshot is published only after it has been saved.

    Parameters
    ----------
    settings : dict
        From ``settings.read_settings``.
    settings_problems : iterable of str
        Its warnings, shown on the settings page.
    """

    def __init__(self, settings: dict, settings_problems=()):
        self.settings = dict(settings)
        self.settings_problems = list(settings_problems)
        self.paused = False
        self.last_error = ""
        self.listener_error = ""  # kept apart: a successful dictation must not hide it
        self.listener_permission: str | None = None  # macOS permission the shortcut lacks
        self._listener_tried_at = 0.0
        self._listener_lock = threading.RLock()  # status polls and the worker may both restart it
        self.last_text = ""
        self.capture = {"status": "none"}  # status: none | waiting | done | timeout | rejected
        self.on_change: list = []
        self.history = History(data_dir() / "history.jsonl")
        self.events: queue.Queue[Event] = queue.Queue()
        self.banner = feedback.Banner()
        self.listener: hotkeys.TriggerListener | None = None
        self.session: ClaudeVoiceSession | None = None
        self.claude_path = ""
        self.claude_version = ""
        self.last_used = time.monotonic()
        self.recovery: list[dict] = []  # transcripts not delivered; in memory only, newest first
        self._recovery_id = 0
        self._settings_lock = threading.RLock()  # one settings change at a time, capture included
        self._phase = "idle"  # idle, starting, listening, transcribing; written by the worker
        self._capturing = False
        # Guards the shortcut's state below, shared by the listener, worker, and page threads.
        # Never held while waiting for another thread or a helper program.
        self._gesture_lock = threading.Lock()
        self._gesture = 0  # number of the latest press
        self._shortcut_held = False
        self._refused_gesture = 0  # a press refused while busy; its release is dropped too
        self._cancelled_through = 0  # presses up to this number must never start recording
        self._cancel_reason: str | None = None  # read by the worker while recording
        self._recording_wanted = False  # a cancel means something only while this is True
        self._delivery_cancel = threading.Event()  # stops a slow copy or typing
        self._mute_lock = threading.Lock()  # a mute in progress always finishes before a restore
        self._stopping = threading.Event()
        self._shutdown_done = False
        self._worker_thread: threading.Thread | None = None
        self._capture_thread: threading.Thread | None = None
        self._announcer: threading.Thread | None = None
        self._banner_close_at = 0.0
        self._warnings_cache: tuple[float, list[dict]] = (-WARNINGS_CACHE_S, [])
        self._last_listener_log = ""
        self._locate_claude()

    # ---- public: called by the app, the tray, and the settings page ----

    @property
    def state(self) -> str:
        """What to show: derived, so pausing and capturing cannot be overwritten by a phase."""
        if self._worker_failed():
            return "failed"
        if self._capturing:
            return "capturing"
        if self._phase != "idle":
            return self._phase
        return "paused" if self.paused else "idle"

    def start(self) -> None:
        system_audio.restore_output()  # in case an earlier copy died while recording
        if IS_MAC:
            mac_events.request_microphone_if_unasked()
        self._start_listener()
        self._worker_thread = threading.Thread(target=self._worker, daemon=True, name="dictation")
        self._worker_thread.start()
        if self.settings["prewarm"]:
            self.events.put(Event(PREWARM))

    def shutdown(self) -> None:
        """Stop everything in a fixed order; bounded, safe to repeat, and safe after a failed start.

        Capture stops first, then the worker, so the mute cannot come back after the
        restore; then the speakers, the session, and the banner. Each step runs even
        if an earlier one failed.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._stopping.set()
        self._delivery_cancel.set()
        self._request_cancel(QUITTING)
        for step in (
            self._stop_listener,
            self._stop_worker,
            self._join_helpers,
            system_audio.restore_output,
            self._stop_session,
            self.banner.close,
        ):
            try:
                step()
            except Exception:
                log.exception("shutdown step failed")

    def set_paused(self, paused: bool) -> None:
        """Pause: the shortcut is ignored, and a recording in progress stops at once."""
        self.paused = paused
        if paused:
            self._request_cancel(PAUSED)
            self._delivery_cancel.set()
        self._notify()

    def update_settings(self, update: dict) -> dict:
        """Check, save, and apply ``update``; returns the full settings.

        Raises
        ------
        ValueError
            If a value is invalid; nothing changes.
        OSError
            If saving fails; nothing changes.
        """
        clean = check_settings(update)
        if "shortcut_key" in clean:
            problem = hotkeys.shortcut_problem(clean["shortcut_key"])
            if problem:
                raise ValueError(problem)
        with self._settings_lock:
            candidate = {**self.settings, **clean}
            changed = {k for k, v in clean.items() if self.settings.get(k) != v}
            save_settings(candidate)
            self.settings = candidate
        if changed & SESSION_SETTINGS:
            self._locate_claude()
            self.events.put(Event(WAKE))  # the worker compares the session with the settings
        if changed & LISTENER_SETTINGS and not self._capturing:
            self._start_listener()
        self._notify()
        return self.settings

    def capture_shortcut(self) -> None:
        """Wait (in the background) for the next key or button press and make it the shortcut."""
        with self._gesture_lock:
            if self._capturing or self._phase != "idle":
                self.capture = {
                    "status": "rejected",
                    "message": "Finish the current dictation first, then try again.",
                }
                return
            self._capturing = True
        self.capture = {"status": "waiting"}
        self._notify()
        self._capture_thread = threading.Thread(target=self._capture, daemon=True, name="capture")
        self._capture_thread.start()

    def open_privacy_settings(self, permission: str) -> None:
        """macOS: open the System Settings page for ``permission``. Raises ValueError."""
        if not IS_MAC or permission not in mac_events.PERMISSIONS:
            raise ValueError(f"unknown permission {permission!r}")
        mac_events.open_privacy_settings(permission)

    def clear_recovery(self, entry_id) -> None:
        """Forget one transcript kept for recovery."""
        self.recovery = [entry for entry in self.recovery if entry["id"] != entry_id]

    def status(self) -> dict:
        """Everything the settings page shows (polled by it every few seconds)."""
        self._retry_blocked_listener()
        settings = self.settings
        session = self.session
        listener = self.listener
        return {
            "platform": "macos" if IS_MAC else "linux",
            "state": self.state,
            "last_error": self.last_error,
            "claude": {"path": self.claude_path, "version": self.claude_version},
            "session_running": bool(session and session.running),
            "shortcut": hotkeys.describe_shortcut(
                settings["shortcut_key"], settings["shortcut_device"]
            ),
            "capture": self.capture,
            "warnings": [
                *(
                    [{"text": self.listener_error, "permission": self.listener_permission}]
                    if self.listener_error
                    else []
                ),
                *(
                    [{"text": listener.warning, "permission": "Accessibility" if IS_MAC else None}]
                    if listener and listener.warning
                    else []
                ),
                *({"text": text, "permission": None} for text in self.settings_problems),
                *self._setup_warnings(settings),
            ],
            "settings": dict(settings),
            "recovery": list(self.recovery),
            "history": self.history.recent(HISTORY_PAGE_SIZE) if settings["history"] else [],
        }

    # ---- shortcut events (from the listener thread) ----

    def _on_down(self) -> None:
        with self._gesture_lock:
            self._gesture += 1
            self._shortcut_held = True
            if self.paused or self._capturing or self._phase == "transcribing":
                self._refused_gesture = self._gesture
                return
            self.events.put(Event(PRESS, self._gesture))

    def _on_up(self) -> None:
        with self._gesture_lock:
            self._shortcut_held = False
            if self._refused_gesture != self._gesture:
                self.events.put(Event(RELEASE, self._gesture))

    def _on_lost(self) -> None:
        """The listener ended or lost track; no release will come for what is held."""
        with self._gesture_lock:
            self._shortcut_held = False
        self._request_cancel(INPUT_LOST)

    def _request_cancel(self, reason: str) -> None:
        with self._gesture_lock:
            self._cancelled_through = self._gesture
            if self._recording_wanted and self._cancel_reason is None:
                self._cancel_reason = reason
        self.events.put(Event(WAKE))

    # ---- internals ----

    def _worker_failed(self) -> bool:
        thread = self._worker_thread
        return bool(thread and not thread.is_alive() and not self._stopping.is_set())

    def _setup_warnings(self, settings: dict) -> list[dict]:
        checked_at, warnings = self._warnings_cache
        if time.monotonic() - checked_at > WARNINGS_CACHE_S:
            warnings = output.setup_warnings(settings)
            self._warnings_cache = (time.monotonic(), warnings)
        return warnings

    def _locate_claude(self) -> None:
        self.claude_path = find_claude(self.settings["claude_path"]) or ""
        self.claude_version = claude_version(self.claude_path) if self.claude_path else ""
        if not self.claude_path:
            self.last_error = NOT_FOUND_HELP
        elif self.last_error == NOT_FOUND_HELP:
            self.last_error = ""

    def _start_listener(self) -> None:
        with self._listener_lock:
            self._stop_listener()
            if self._stopping.is_set():
                return
            settings = self.settings
            problem = hotkeys.shortcut_problem(settings["shortcut_key"])
            try:
                if problem:
                    raise ValueError(problem)
                listener = hotkeys.TriggerListener(
                    settings, self._on_down, self._on_up, self._on_lost
                )
                listener.start()
                self.listener = listener
                self.listener_error = ""
                self.listener_permission = None
            except (ValueError, OSError) as error:
                self._note_listener_failure(error)
            except Exception as error:  # evdev's UInputError derives from Exception only
                if f"Shortcut not active: {error}" != self._last_listener_log:  # retried often
                    log.exception("the shortcut listener could not start")
                self._note_listener_failure(error)
            self._listener_tried_at = time.monotonic()

    def _note_listener_failure(self, error: Exception) -> None:
        self.listener_error = f"Shortcut not active: {error}"
        self.listener_permission = getattr(error, "permission", None)
        if self.listener_error != self._last_listener_log:
            log.warning(self.listener_error)
        self._last_listener_log = self.listener_error

    def _stop_listener(self) -> None:
        with self._listener_lock:
            listener, self.listener = self.listener, None
            if listener:
                listener.stop()

    def _listener_needs_restart(self) -> bool:
        listener = self.listener
        if IS_MAC:
            if listener and not mac_events.can_listen():
                # Revoked while running: macOS just stops sending events. Say so, and
                # end anything waiting for a release.
                self._stop_listener()
                self.listener_error = mac_events.permission_help("Input Monitoring")
                self.listener_permission = "Input Monitoring"
                return False
            if self.listener_permission:
                return mac_events.can_listen()
            if listener and listener.warning and mac_events.can_type():
                return True  # Accessibility was granted: restart to hide the key after all
            return bool(self.listener_error) or bool(listener and not listener.is_alive())
        # Linux: the device was missing at start, or went away (unplugged, suspend).
        return bool(self.listener_error) or bool(listener and not listener.is_alive())

    def _retry_blocked_listener(self) -> None:
        """Restart the shortcut once it can work again, without restarting the app."""
        if not self._listener_lock.acquire(blocking=False):
            return  # another thread is already (re)starting it
        try:
            if (
                not self._capturing
                and time.monotonic() - self._listener_tried_at > LISTENER_RETRY_INTERVAL_S
            ):
                self._listener_tried_at = time.monotonic()
                if self._listener_needs_restart():
                    self._start_listener()
        finally:
            self._listener_lock.release()

    def _capture(self) -> None:
        """Before: the old listener runs. After: a listener runs for whichever shortcut is saved."""
        try:
            self._stop_listener()
            found = hotkeys.capture_next_input(CAPTURE_TIMEOUT_S, self._stopping)
            if found is None:
                self.capture = {"status": "timeout"}
            elif problem := hotkeys.shortcut_problem(found[0]):
                self.capture = {"status": "rejected", "message": problem}
            else:
                key, device = found
                with self._settings_lock:
                    candidate = {**self.settings, "shortcut_key": key, "shortcut_device": device}
                    save_settings(candidate)
                    self.settings = candidate
                self.capture = {
                    "status": "done",
                    "shortcut": hotkeys.describe_shortcut(key, device),
                }
                log.info("new shortcut %s", self.capture["shortcut"])
        except OSError as error:
            log.warning("could not capture a shortcut: %s", type(error).__name__)
            permission = getattr(error, "permission", None)
            message = str(error) if permission else f"Could not save it: {error.strerror}."
            self.capture = {"status": "rejected", "message": message}
        except Exception:
            log.exception("could not capture a shortcut")
            self.capture = {"status": "rejected", "message": "That did not work; try again."}
        finally:
            self._capturing = False
            self._start_listener()
            self._notify()

    def _set_phase(self, phase: str) -> None:
        with self._gesture_lock:
            self._phase = phase
        self._notify()

    def _notify(self) -> None:
        for callback in self.on_change:
            try:
                callback(self)
            except Exception:  # a broken indicator must never stop dictation
                log.exception("state callback failed")

    def _session_matches(self, settings: dict) -> bool:
        session = self.session
        return (
            session is not None
            and session.claude_path == self.claude_path
            and session.language == settings["language"]
        )

    def _ensure_session(self, settings: dict) -> None:
        """Have a session ready for ``settings``. It may take seconds; presses queue meanwhile."""
        session = self.session
        if session and session.is_alive():
            if self._session_matches(settings) and session.wait_ready_for_recording():
                return
            session.stop()  # settings changed, or it shows a screen no Space should reach
        if not self.claude_path:
            raise DictationError(self.last_error or NOT_FOUND_HELP)
        if self._stopping.is_set():
            raise DictationError("The app is quitting.")
        self._set_phase("starting")
        self._banner("Starting Claude Code…", settings)
        self.session = ClaudeVoiceSession(self.claude_path, settings["language"])
        self.session.start()

    def _stop_session(self) -> None:
        session = self.session
        if session:
            session.stop()

    def _stop_worker(self) -> None:
        thread = self._worker_thread
        if not thread:
            return
        thread.join(SHUTDOWN_WORKER_WAIT_S)
        if thread.is_alive():
            self._stop_session()  # it is waiting on Claude Code; this ends the wait
            thread.join(SHUTDOWN_JOIN_TIMEOUT_S)
        if thread.is_alive():
            log.warning("the dictation worker did not stop in time")

    def _join_helpers(self) -> None:
        for thread in (self._capture_thread, self._announcer):
            if thread and thread.is_alive():
                thread.join(THREAD_JOIN_TIMEOUT_S)

    def _worker(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    event = self.events.get(timeout=self._seconds_until_housekeeping())
                except queue.Empty:
                    try:
                        self._housekeeping()
                    except Exception:
                        log.exception("housekeeping failed")
                    continue
                self._handle_safely(event)
        except BaseException:
            log.exception("the dictation worker stopped")
            self.last_error = "Dictation stopped working. Restart the app; the log has details."
            self._notify()
            raise

    def _handle_safely(self, event: Event) -> None:
        """Handle one event; only a real recoverable failure ends it, and the worker lives on."""
        try:
            self._handle(event)
        except PartialTranscript as error:
            self._keep_for_recovery(error.text, str(error))
            self._report(f"{error} The text is kept on the settings page.")
        except DictationError as error:
            if not self._stopping.is_set():
                self._report(str(error))
        except UnsafeFolderError as error:
            self._report(str(error))
        except (OSError, UnicodeError, subprocess.SubprocessError) as error:
            # Terminal and file failures: this session can no longer be trusted.
            self._report(f"Dictation failed ({type(error).__name__}). The next press starts over.")
            self._discard_session()
        except Exception:
            log.exception("dictation failed")  # a bug: keep the traceback, keep dictation alive
            self._report("Something went wrong in this app; the log has details.")
            self._discard_session()
        finally:
            self._set_phase("idle")
            self._close_banner_unless_error()

    def _handle(self, event: Event) -> None:
        if event.kind == PRESS:
            self._dictate_once(event)
        elif event.kind == PREWARM:
            self._ensure_session(self.settings)
            self.session.check_logged_in()
            if self.last_error != NOT_FOUND_HELP:
                self.last_error = ""
        if not self._stopping.is_set():
            self._apply_session_settings()

    def _apply_session_settings(self) -> None:
        """Between recordings: replace a session whose language or program has changed."""
        settings = self.settings
        session = self.session
        if session and session.running and not self._session_matches(settings):
            session.stop()
            if settings["prewarm"]:
                self.events.put(Event(PREWARM))

    def _discard_session(self) -> None:
        try:
            self._stop_session()
        except Exception:
            log.exception("could not stop the session")

    def _seconds_until_housekeeping(self) -> float:
        if self._banner_close_at:
            return max(0.0, min(IDLE_CHECK_INTERVAL_S, self._banner_close_at - time.monotonic()))
        return IDLE_CHECK_INTERVAL_S

    def _housekeeping(self) -> None:
        if self._banner_close_at and time.monotonic() >= self._banner_close_at:
            self._banner_close_at = 0.0
            self.banner.close()
            return
        self._stop_if_idle()
        self._retry_blocked_listener()  # also while the settings page is closed

    def _stop_if_idle(self) -> None:
        idle_minutes = self.settings["idle_shutdown_minutes"]
        idle_s = time.monotonic() - self.last_used
        if idle_minutes and idle_s > idle_minutes * 60 and self.session and self.session.is_alive():
            log.info("idle for %.0f min, stopping the hidden Claude Code", idle_s / 60)
            self.session.stop()
            self._notify()

    def _report(self, message: str) -> None:
        """Tell the user about a problem. The message must hold no transcript or terminal text."""
        self.last_error = message
        log.warning(message)
        self._banner(f"⚠ {message}", self.settings)
        self._banner_close_at = time.monotonic() + ERROR_BANNER_S

    def _close_banner_unless_error(self) -> None:
        if not self._banner_close_at:
            self.banner.close()

    def _keep_for_recovery(self, text: str, reason: str) -> None:
        """Keep undelivered text in memory, whatever the history setting; never on disk."""
        self._recovery_id += 1
        entry = {
            "id": self._recovery_id,
            "text": text,
            "reason": reason,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        self.recovery = [entry, *self.recovery][:MAX_RECOVERY_ENTRIES]

    def _press_still_wanted(self, press: Event, toggle: bool) -> bool:
        """After any wait: is this press still meant to record, right now?"""
        with self._gesture_lock:
            if self.paused or self._stopping.is_set() or press.gesture <= self._cancelled_through:
                return False
            if not toggle:
                return self._shortcut_held and self._gesture == press.gesture
            # Toggle mode: the key is up again already; a newer press means "stop".
            return self._gesture == press.gesture

    def _dictate_once(self, press: Event) -> None:
        settings = self.settings  # one snapshot for the whole recording
        toggle = settings["mode"] == "toggle"
        recorded = False
        try:
            if not self._press_still_wanted(press, toggle):
                return
            with self._gesture_lock:
                self._recording_wanted = True
                self._cancel_reason = None
            self.last_used = time.monotonic()
            # A cold start takes seconds; the press may end meanwhile.
            self._ensure_session(settings)
            if not self._press_still_wanted(press, toggle):
                log.info("the press ended before Claude Code was ready; not recording")
                return
            recorded = True
            seconds, target_app = self._record(press, settings, toggle)
            with self._gesture_lock:
                self._phase = "transcribing"  # from here on, new presses are refused
                self._recording_wanted = False
                reason = self._cancel_reason
            self._notify()
            self._finish_recording(seconds, target_app, settings, reason)
        finally:
            with self._gesture_lock:
                self._recording_wanted = False
                self._cancel_reason = None
                # After a recording everything queued is over. A hold-mode press abandoned
                # during a wait drops only itself, because pressing again while Claude Code
                # starts still counts. In toggle mode a second press meant "stop".
                newest = self._gesture if recorded or toggle else press.gesture
            self._drop_queued_gestures(newest)

    def _drop_queued_gestures(self, through: int) -> None:
        """Presses up to ``through`` are over; they must not start a recording later."""
        kept = []
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event.kind not in (PRESS, RELEASE) or event.gesture > through:
                kept.append(event)
        for event in kept:
            self.events.put(event)

    def _record(self, press: Event, settings: dict, toggle: bool) -> tuple[float, int | None]:
        """Hold Space until the stop signal.

        Before: shortcut down, prompt box ready. After: Spaces stopped, speakers
        back. Claude Code needs ~0.15 s of repeated Spaces before it records, and
        words spoken before that are lost. So the Spaces start at once, and the tray,
        banner, sound, and mute (~0.3 s together) run beside them.

        Returns
        -------
        (float, int or None)
            Seconds held, and the app in front at release (see ``output.focused_app``).
        """
        session = self.session
        started = time.monotonic()
        session.begin_hold()
        self._phase = "listening"
        recording_over = threading.Event()
        self._announcer = threading.Thread(
            target=self._announce_recording,
            args=(settings, toggle, recording_over),
            daemon=True,
            name="announce",
        )
        self._announcer.start()
        try:
            self._hold_space(session, press, toggle, started)
        finally:
            recording_over.set()
            self._announcer.join(ANNOUNCER_JOIN_TIMEOUT_S)
            # Recording is over; transcribing needs no silence, so sound comes back at once.
            with self._mute_lock:
                system_audio.restore_output()
        # The text belongs in the app in front now, even if you switch while it transcribes.
        return time.monotonic() - started, output.focused_app()

    def _hold_space(self, session, press: Event, toggle: bool, started: float) -> None:
        # Hold mode stops on this press's release; toggle mode ignores releases and
        # stops on the next press. A cancel is seen within one key repeat, and so is a
        # voice failure, because another Space would make Claude Code connect again.
        while time.monotonic() - started < MAX_RECORDING_S and self._cancel_reason is None:
            if session.voice_failed():
                log.info("voice reported a failure; no more Spaces for this press")
                return
            session.press_space()
            try:
                event = self.events.get(timeout=KEY_REPEAT_INTERVAL_S)
            except queue.Empty:
                continue
            if toggle and event.kind == PRESS:
                return
            if not toggle and event.kind == RELEASE and event.gesture == press.gesture:
                return

    def _finish_recording(
        self, seconds: float, target_app: int | None, settings: dict, reason: str | None
    ) -> None:
        session = self.session
        if reason == QUITTING:
            return  # shutdown stops the session, which discards the text
        if reason == PAUSED:
            session.cancel_recording()
            log.info("recording cancelled by pause")
            return
        self._banner("✍ Transcribing…", settings)
        if settings["sounds"]:
            feedback.play_sound("stop")
        text = session.collect_transcript()
        log.info("recorded %.1f s, got %d characters", seconds, len(text))
        self.last_used = time.monotonic()
        if not text:
            return
        if reason == INPUT_LOST:
            self._keep_for_recovery(
                text, "The shortcut was lost while recording, so nothing was typed."
            )
            self._report("The shortcut was lost while recording. The text is on the settings page.")
            return
        self._deliver(text, seconds, target_app, settings)

    def _deliver(self, text: str, seconds: float, target_app, settings: dict) -> None:
        """History, clipboard, and insertion succeed or fail separately; nothing here retries."""
        self.last_text = text
        self.last_error = ""
        problems = []
        if settings["history"]:
            try:
                self.history.add(text, seconds)
            except OSError as error:
                log.warning("could not save to the history: %s", error.strerror)
                problems.append("Could not save it to the history.")
        self._delivery_cancel.clear()
        if self.paused or self._stopping.is_set():  # checked after the clear, so none is missed
            self._keep_for_recovery(text, "The app was paused before the text was typed.")
            return
        if settings["output"] != "none" and not self._wait_for_shortcut_release():
            self._keep_for_recovery(text, "The shortcut was still held, so it was not typed.")
            problems.append("The shortcut was still held, so it was not typed.")
        else:
            result = output.deliver(text, settings, target_app, self._delivery_cancel)
            if result.problem:
                self._keep_for_recovery(text, result.problem)
                problems.append(result.problem)
            elif settings["output"] == "none" and not (
                settings["copy_to_clipboard"] or settings["history"]
            ):
                # Every way out is switched off; this page is the only place left for it.
                self._keep_for_recovery(text, "Typing, copying, and history are all off.")
        if problems:
            self._report(" ".join(problems))

    def _wait_for_shortcut_release(self) -> bool:
        deadline = time.monotonic() + RELEASE_WAIT_S
        while (listener := self.listener) and listener.held_keys():
            if time.monotonic() > deadline:
                return False
            time.sleep(RELEASE_POLL_S)
        return True

    def _announce_recording(self, settings: dict, toggle: bool, over: threading.Event) -> None:
        """Show "listening" (tray, banner, sound), then mute the speakers if set.

        Runs beside the Spaces and only gives feedback: each step is skipped once
        the recording is over, and it never changes the phase.
        """
        self._notify()
        if over.is_set():
            return
        self._banner(
            "🎙 Listening… " + ("(press again to finish)" if toggle else "(release to finish)"),
            settings,
        )
        mute = settings["mute_while_dictating"]
        if settings["sounds"] and not over.is_set():
            # Let the start sound finish first, or muting would cut it off.
            feedback.play_sound("start", wait=mute)
        if mute:
            self._mute_while(over)

    def _mute_while(self, over: threading.Event) -> None:
        with self._mute_lock:
            if not over.is_set():
                system_audio.mute_output()

    def _banner(self, title: str, settings: dict) -> None:
        if settings["notifications"]:
            self._banner_close_at = 0.0
            self.banner.show(title)
