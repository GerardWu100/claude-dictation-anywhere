"""Local history of transcripts, one JSON object per line, newest last.

Each line: {"time": "2026-10-25T22:12:29", "text": "...", "seconds": 5.4}
The file stays on this computer (see settings.data_dir()).
"""

import json
import threading
from datetime import datetime
from pathlib import Path

# Entries kept; older ones are dropped when the file grows past this.
MAX_HISTORY_ENTRIES = 500


class History:
    def __init__(self, path: Path, max_entries: int = MAX_HISTORY_ENTRIES):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()

    def add(self, text: str, seconds: float) -> dict:
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "text": text,
            "seconds": round(seconds, 1),
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            entries = self._read()
            if len(entries) > self.max_entries:
                self._write(entries[-self.max_entries :])
        return entry

    def recent(self, limit: int) -> list[dict]:
        """Newest first."""
        with self._lock:
            return list(reversed(self._read()[-limit:]))

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line cut short by a crash
        return entries

    def _write(self, entries: list[dict]) -> None:
        partial = self.path.with_suffix(".partial")
        partial.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
            encoding="utf-8",
        )
        partial.replace(self.path)
