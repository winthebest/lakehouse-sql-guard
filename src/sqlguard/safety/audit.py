"""Append-only JSONL audit log: a "completed" line per submission, and a "started" line before
any submission that has side effects.

Each record is written with a single `write` on an `O_APPEND` descriptor and fsynced, so
concurrent writers cannot interleave lines and a crash cannot truncate an earlier record. The
log is never rewritten by this module.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any


class AuditError(RuntimeError):
    """The audit log cannot be written. The guard refuses to act without an audit trail."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def ensure_writable(self) -> None:
        """Raise AuditError unless a record can be appended (called before anything runs)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        except OSError as err:
            raise AuditError(f"audit log {self.path} is not writable: {err}") from err
        os.close(fd)

    def append(self, record: dict[str, Any]) -> str:
        """Write `record` (adding `id` and `ts` when absent) and return its id."""
        entry = {"id": uuid.uuid4().hex, "ts": datetime.now(UTC).isoformat(), **record}
        line = self.render(entry) + "\n"
        try:
            with self._lock:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    data = line.encode("utf-8")
                    if os.write(fd, data) != len(data):
                        raise OSError("short write")
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as err:
            raise AuditError(f"cannot append to audit log {self.path}: {err}") from err
        return str(entry["id"])

    @staticmethod
    def render(record: dict[str, Any]) -> str:
        """The JSON text of one record (without the trailing newline)."""
        return json.dumps(record, default=_jsonable, sort_keys=False, ensure_ascii=False)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
