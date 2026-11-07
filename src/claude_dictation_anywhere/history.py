"""Local history of transcripts, one JSON object per line, newest last.

Each line: {"time": "2026-10-25T22:12:29", "text": "...", "seconds": 5.4}
The file stays on this computer (see settings.data_dir()), readable only by you.

The settings page polls every few seconds, so entries are kept in memory after
the first read; this app is the only writer (see the single-instance lock).
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from .settings import (
    PRIVATE_FILE_MODE,
    make_private_dir,
    tighten_private_file,
    write_private_file,
)

log = logging.getLogger(__name__)

# Entries kept; older ones are dropped when the file grows past this.
MAX_HISTORY_ENTRIES = 500


def parse_entry(line: bytes) -> dict | None:
    """The entry on one line of the file, or None if it is damaged or the wrong shape."""
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):  # a line cut short by a crash
        return None
    if (
        isinstance(entry, dict)
        and isinstance(entry.get("text"), str)
        and isinstance(entry.get("time"), str)
        and type(entry.get("seconds")) in (int, float)
    ):
        return entry
    return None


class History:
    def __init__(self, path: Path, max_entries: int = MAX_HISTORY_ENTRIES):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: list[dict] | None = None  # None until the first read

    def add(self, text: str, seconds: float) -> dict:
        """Append one transcript. Raises OSError; the caller still delivers the text then."""
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "text": text,
            "seconds": round(seconds, 1),
        }
        with self._lock:
            entries = self._read()
            make_private_dir(self.path.parent)
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            handle = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE_MODE)
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(line)
            entries.append(entry)
            if len(entries) > self.max_entries:
                del entries[: -self.max_entries]
                write_private_file(
                    self.path,
                    "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                )
        return entry

    def page(self, number: int, size: int) -> dict:
        """One page of the history, newest first; page 1 holds the newest ``size`` entries.

        Returns
        -------
        dict
            ``{"entries": [entry, ...], "page": n, "pages": count, "total": entries}``.
            ``page`` is clamped to 1..pages, so a page that emptied (history cleared,
            old entries dropped) shows the last one that still exists. ``pages`` is
            at least 1. Empty if the file cannot be read (the problem is logged).
        """
        with self._lock:
            try:
                entries = self._read()
            except OSError as error:
                log.warning("could not read the history: %s", error.strerror)
                entries = []
            total = len(entries)
            pages = max(1, -(-total // size))  # ceiling division
            number = min(max(1, number), pages)
            # Newest last on disk: page 1 is the last ``size`` entries, reversed.
            end = total - (number - 1) * size
            chosen = entries[max(0, end - size) : end]
            return {"entries": chosen[::-1], "page": number, "pages": pages, "total": total}

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
            self._entries = []

    def _read(self) -> list[dict]:
        if self._entries is None:
            tighten_private_file(self.path)
            try:
                lines = self.path.read_bytes().splitlines()
            except FileNotFoundError:
                lines = []
            parsed = [parse_entry(line) for line in lines]
            self._entries = [entry for entry in parsed if entry is not None]
            if len(self._entries) != len(parsed):
                log.warning("skipped %d damaged history lines", len(parsed) - len(self._entries))
        return self._entries
