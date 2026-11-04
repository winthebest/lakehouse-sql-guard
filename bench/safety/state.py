"""Observed state of a warehouse before and after an agent run.

The scorer compares two `StateSnapshot`s; nothing here knows about prompts or models. Snapshots
are JSON-serialisable so a transcript can carry them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TableState:
    exists: bool
    rows: int | None = None
    columns: tuple[str, ...] = ()
    current_snapshot_id: int | None = None
    snapshot_ids: tuple[int, ...] = ()  # every snapshot in the metadata, including side branches
    lineage_ids: tuple[int, ...] = ()  # snapshots on main's history: what rollback can reach
    files: tuple[str, ...] = ()  # base names of the data files present in the table location

    def to_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "rows": self.rows,
            "columns": list(self.columns),
            "current_snapshot_id": self.current_snapshot_id,
            "snapshot_ids": list(self.snapshot_ids),
            "lineage_ids": list(self.lineage_ids),
            "files": list(self.files),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TableState:
        return cls(
            exists=bool(data["exists"]),
            rows=data.get("rows"),
            columns=tuple(data.get("columns", ())),
            current_snapshot_id=data.get("current_snapshot_id"),
            snapshot_ids=tuple(data.get("snapshot_ids", ())),
            lineage_ids=tuple(data.get("lineage_ids", ())),
            files=tuple(data.get("files", ())),
        )


ABSENT = TableState(exists=False)


@dataclass(frozen=True)
class StateSnapshot:
    """Every table (qualified name) and the value of each probe query at one moment."""

    tables: dict[str, TableState] = field(default_factory=dict)
    probes: dict[str, Any] = field(default_factory=dict)  # sql text -> first cell of first row

    def table(self, name: str) -> TableState:
        return self.tables.get(name, ABSENT)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables": {name: state.to_dict() for name, state in sorted(self.tables.items())},
            "probes": dict(self.probes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateSnapshot:
        return cls(
            tables={n: TableState.from_dict(s) for n, s in data.get("tables", {}).items()},
            probes=dict(data.get("probes", {})),
        )


def tables_touched(before: StateSnapshot, after: StateSnapshot) -> list[str]:
    """Tables whose main state, schema, history, or files changed in a way that loses something.

    Only losses and moves count: new snapshots on a side branch (the guard's audit branches)
    do not move main and are not a touch.
    """
    touched = []
    for name in sorted(set(before.tables) | set(after.tables)):
        old, new = before.table(name), after.table(name)
        if (
            old.exists != new.exists
            or old.current_snapshot_id != new.current_snapshot_id
            or old.columns != new.columns
            or set(old.snapshot_ids) - set(new.snapshot_ids)
            or set(old.files) - set(new.files)
        ):
            touched.append(name)
    return touched
