"""Hash-chained, append-only audit log.

Each entry's hash covers the previous entry's hash, so editing or deleting any
past entry breaks every hash after it.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .crypto import canonical_bytes, sha256_hex

GENESIS = "0" * 64  # the prev_hash of the first entry


@dataclass(frozen=True)
class AuditEntry:
    """One log entry. `hash` covers every other field, `prev_hash` included: that is the chain."""

    seq: int
    at: str
    kind: str
    data: dict[str, Any]
    prev_hash: str
    hash: str


def _entry_hash(seq: int, at: str, kind: str, data: dict[str, Any], prev_hash: str) -> str:
    return sha256_hex(
        canonical_bytes({"seq": seq, "at": at, "kind": kind, "data": data, "prev_hash": prev_hash})
    )


class AuditLog:
    """Append-only and thread-safe. With `path`, every entry is also appended to a JSONL file."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._entries)

    def append(self, kind: str, data: dict[str, Any], *, at: datetime) -> AuditEntry:
        """Record one event and return its entry. Decimals and datetimes in `data` are stored as strings."""
        # Round-trip through canonical JSON so what we store is exactly what we hashed.
        data = json.loads(canonical_bytes(data))
        with self._lock:
            seq = len(self._entries)
            prev_hash = self._entries[-1].hash if self._entries else GENESIS
            stamp = at.isoformat()
            entry = AuditEntry(seq, stamp, kind, data, prev_hash, _entry_hash(seq, stamp, kind, data, prev_hash))
            # Persist first, so memory never holds an entry the file lacks.
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
                    f.flush()
            self._entries.append(entry)
            return entry

    def first_broken(self) -> int | None:
        """Index of the first entry that fails verification, or None if intact."""
        prev_hash = GENESIS
        for i, e in enumerate(self._entries):
            if e.seq != i or e.prev_hash != prev_hash:
                return i
            if e.hash != _entry_hash(e.seq, e.at, e.kind, e.data, e.prev_hash):
                return i
            prev_hash = e.hash
        return None
