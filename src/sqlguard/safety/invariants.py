"""Invariants a change must satisfy before it is published (and again once it is live on main).

An `Invariant` inspects the table before and after a change (`InvariantContext`) and returns a
failure message or None. Built-in checks are configurable per table in the policy YAML:

    invariants:
      default:
        max_delete_pct: 50          # at most this % of the existing rows may be deleted
        max_row_decrease_pct: 50    # net row count may not shrink by more than this %
        min_rows: 1                 # table must keep at least this many rows
        max_rows: 1000000
        not_null: [id]              # columns that must contain no NULL
        unique_key: [id]            # columns that must be unique (also keys the impact diff)
      tables:
        orders: {max_delete_pct: 10, not_null: [id, customer]}

Table entries override the default field by field. Callers can add arbitrary code invariants
through `Guard(extra_invariants=...)`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import duckdb
import pyarrow as pa

from sqlguard.safety.actions import qualify_table_name
from sqlguard.safety.impact import TableImpact, quote_ident

Phase = Literal["pre_publish", "post_publish"]
BOTH: frozenset[Phase] = frozenset({"pre_publish", "post_publish"})


class InvariantConfigError(ValueError):
    """The invariants configuration is invalid."""


@dataclass(frozen=True)
class InvariantContext:
    table: str
    before: pa.Table
    after: pa.Table
    impact: TableImpact


@dataclass(frozen=True)
class InvariantResult:
    name: str
    table: str
    phase: Phase
    passed: bool
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table": self.table,
            "phase": self.phase,
            "passed": self.passed,
            "message": self.message,
        }


@dataclass(frozen=True)
class Invariant:
    """A named check. `check` returns a failure message, or None when the invariant holds."""

    name: str
    check: Callable[[InvariantContext], str | None]
    phases: frozenset[Phase] = BOTH


def evaluate(
    invariants: list[Invariant], ctx: InvariantContext, phase: Phase
) -> list[InvariantResult]:
    """Run every invariant active in `phase`. A check that raises counts as failed (fail closed)."""
    results = []
    for inv in invariants:
        if phase not in inv.phases:
            continue
        try:
            failure = inv.check(ctx)
        except Exception as err:
            failure = f"check raised {type(err).__name__}: {err}"
        results.append(InvariantResult(inv.name, ctx.table, phase, failure is None, failure or ""))
    return results


# -- built-in checks ---------------------------------------------------------------------------


def max_delete_pct(limit: float) -> Invariant:
    def check(ctx: InvariantContext) -> str | None:
        pct = ctx.impact.deleted_pct
        if pct is not None and pct > limit:
            return f"{pct:.1f}% of rows deleted (limit {limit:g}%)"
        return None

    return Invariant(f"max_delete_pct<={limit:g}", check)


def max_row_decrease_pct(limit: float) -> Invariant:
    def check(ctx: InvariantContext) -> str | None:
        before, after = ctx.impact.rows_before, ctx.impact.rows_after
        if before and (drop := 100.0 * (before - after) / before) > limit:
            return f"row count fell {drop:.1f}% from {before} to {after} (limit {limit:g}%)"
        return None

    return Invariant(f"max_row_decrease_pct<={limit:g}", check)


def min_rows(minimum: int) -> Invariant:
    def check(ctx: InvariantContext) -> str | None:
        rows = ctx.impact.rows_after
        return f"{rows} rows remain (minimum {minimum})" if rows < minimum else None

    return Invariant(f"min_rows>={minimum}", check)


def max_rows(maximum: int) -> Invariant:
    def check(ctx: InvariantContext) -> str | None:
        rows = ctx.impact.rows_after
        return f"{rows} rows exceed the maximum {maximum}" if rows > maximum else None

    return Invariant(f"max_rows<={maximum}", check)


def not_null(columns: tuple[str, ...]) -> Invariant:
    def check(ctx: InvariantContext) -> str | None:
        problems = []
        for column in columns:
            if column not in ctx.after.column_names:
                problems.append(f"{column} is missing")
            elif nulls := ctx.after.column(column).null_count:
                problems.append(f"{column} has {nulls} NULL values")
        return "; ".join(problems) or None

    return Invariant(f"not_null({', '.join(columns)})", check)


def unique_key(columns: tuple[str, ...]) -> Invariant:
    def check(ctx: InvariantContext) -> str | None:
        missing = [c for c in columns if c not in ctx.after.column_names]
        if missing:
            return f"key column(s) missing: {', '.join(missing)}"
        cols = ", ".join(quote_ident(c) for c in columns)
        conn = duckdb.connect()
        try:
            conn.register("after_t", ctx.after)
            row = conn.execute(
                "SELECT count(*) FROM "
                f"(SELECT {cols} FROM after_t GROUP BY {cols} HAVING count(*) > 1)"
            ).fetchone()
        finally:
            conn.close()
        dupes = int(row[0]) if row else 0
        return f"{dupes} duplicated key value(s) in ({', '.join(columns)})" if dupes else None

    return Invariant(f"unique_key({', '.join(columns)})", check)


# -- configuration -----------------------------------------------------------------------------

_FIELDS = {
    "max_delete_pct",
    "max_row_decrease_pct",
    "min_rows",
    "max_rows",
    "not_null",
    "unique_key",
}


@dataclass(frozen=True)
class TableInvariants:
    """Built-in invariant settings for one table; None / empty means 'not configured'."""

    max_delete_pct: float | None = None
    max_row_decrease_pct: float | None = None
    min_rows: int | None = None
    max_rows: int | None = None
    not_null: tuple[str, ...] = ()
    unique_key: tuple[str, ...] = ()

    def overlay(self, base: TableInvariants) -> TableInvariants:
        """Settings of `self` where configured, else those of `base`."""
        values = {f: getattr(self, f) for f in _FIELDS}
        merged = {
            f: v if (v is not None and v != ()) else getattr(base, f) for f, v in values.items()
        }
        return replace(self, **merged)

    def build(self) -> list[Invariant]:
        found: list[Invariant] = []
        if self.max_delete_pct is not None:
            found.append(max_delete_pct(self.max_delete_pct))
        if self.max_row_decrease_pct is not None:
            found.append(max_row_decrease_pct(self.max_row_decrease_pct))
        if self.min_rows is not None:
            found.append(min_rows(self.min_rows))
        if self.max_rows is not None:
            found.append(max_rows(self.max_rows))
        if self.not_null:
            found.append(not_null(self.not_null))
        if self.unique_key:
            found.append(unique_key(self.unique_key))
        return found


@dataclass(frozen=True)
class InvariantsConfig:
    default: TableInvariants = field(default_factory=TableInvariants)
    tables: dict[str, TableInvariants] = field(default_factory=dict)

    def for_table(self, table: str) -> TableInvariants:
        specific = self.tables.get(table)
        return specific.overlay(self.default) if specific else self.default

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, namespace: str) -> InvariantsConfig:
        if not data:
            return cls()
        if not isinstance(data, Mapping):
            raise InvariantConfigError("invariants must be a mapping")
        unknown = set(data) - {"default", "tables"}
        if unknown:
            raise InvariantConfigError(f"unknown invariants keys: {sorted(unknown)}")
        tables_cfg = data.get("tables") or {}
        if not isinstance(tables_cfg, Mapping):
            raise InvariantConfigError("invariants.tables must be a mapping")
        return cls(
            default=_table_invariants(data.get("default") or {}),
            tables={
                qualify_table_name(str(name), namespace): _table_invariants(cfg or {})
                for name, cfg in tables_cfg.items()
            },
        )


def _table_invariants(cfg: Mapping[str, Any]) -> TableInvariants:
    if not isinstance(cfg, Mapping):
        raise InvariantConfigError("invariant settings must be a mapping")
    unknown = set(cfg) - _FIELDS
    if unknown:
        raise InvariantConfigError(f"unknown invariant settings: {sorted(unknown)}")

    def number(name: str, maximum: float | None = None) -> float | None:
        value = cfg.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise InvariantConfigError(f"{name} must be a number >= 0")
        if maximum is not None and value > maximum:
            raise InvariantConfigError(f"{name} must be <= {maximum:g}")
        return float(value)

    def columns(name: str) -> tuple[str, ...]:
        value = cfg.get(name)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise InvariantConfigError(f"{name} must be a list of column names")
        return tuple(value)

    min_r, max_r = number("min_rows"), number("max_rows")
    return TableInvariants(
        max_delete_pct=number("max_delete_pct", 100),
        max_row_decrease_pct=number("max_row_decrease_pct", 100),
        min_rows=None if min_r is None else int(min_r),
        max_rows=None if max_r is None else int(max_r),
        not_null=columns("not_null"),
        unique_key=columns("unique_key"),
    )


def invariants_for(
    config: InvariantsConfig, table: str, extra: list[Invariant] | tuple[Invariant, ...] = ()
) -> list[Invariant]:
    return [*config.for_table(table).build(), *extra]
