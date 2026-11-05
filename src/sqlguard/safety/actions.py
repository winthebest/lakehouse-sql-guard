"""Actions an agent can submit to the guard: raw SQL or structured maintenance operations.

The guard is static: nothing here executes. Structured maintenance actions exist because
Iceberg maintenance (expire snapshots, remove orphan files, rollback) is usually driven by tool
calls rather than SQL. Spark `CALL system.<procedure>(...)` text is also accepted inside a
`SqlAction`; `parse_spark_call` converts it to the matching maintenance action and raises
`UnsupportedCall` for anything it cannot verify, which the classifier maps to UNKNOWN (DENY).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlguard.core.parsing import DEFAULT_DIALECT


@dataclass(frozen=True)
class SqlAction:
    """One or more SQL statements submitted as a single action."""

    sql: str
    dialect: str = DEFAULT_DIALECT


@dataclass(frozen=True)
class MaintenanceAction:
    """Base for structured table-maintenance operations. `table` may be `name` or `ns.name`."""

    table: str


@dataclass(frozen=True)
class ExpireSnapshots(MaintenanceAction):
    """Drop snapshots older than `older_than`, always keeping the newest `retain_last`.

    `older_than` is required to be a concrete timestamp; omitting it makes retention unverifiable.
    `retain_last=None` means the engine default (1).
    """

    older_than: datetime | None = None
    retain_last: int | None = None
    snapshot_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RemoveOrphanFiles(MaintenanceAction):
    """Physically delete files no snapshot references that are older than `older_than`."""

    older_than: datetime | None = None


@dataclass(frozen=True)
class RollbackToSnapshot(MaintenanceAction):
    """Move the table's main branch pointer to an earlier snapshot, by id, timestamp, or ref."""

    snapshot_id: int | None = None
    as_of: datetime | None = None
    ref: str | None = None


@dataclass(frozen=True)
class FastForward(MaintenanceAction):
    """Publish a branch by fast-forwarding `branch` to the state of `to` (metadata only)."""

    branch: str = "main"
    to: str = ""


@dataclass(frozen=True)
class DropTable(MaintenanceAction):
    """Drop a table; `purge=True` also deletes its data files."""

    purge: bool = False


@dataclass(frozen=True)
class ColumnChange:
    """One top-level column change of an `ALTER TABLE`: `drop` it or `rename` it to `new_name`."""

    op: Literal["drop", "rename"]
    column: str
    new_name: str | None = None

    def describe(self) -> str:
        return (
            f"drop column {self.column}"
            if self.op == "drop"
            else f"rename column {self.column} to {self.new_name}"
        )


type Action = SqlAction | MaintenanceAction


class UnsupportedCall(ValueError):
    """A `CALL` statement is malformed, unknown, or uses arguments the guard cannot verify."""


# Positional parameter order per Iceberg Spark procedure (system.<name>). Arguments outside this
# list are rejected rather than ignored, because an unknown argument may change behaviour.
_PROCEDURE_PARAMS: dict[str, tuple[str, ...]] = {
    "expire_snapshots": (
        "table",
        "older_than",
        "retain_last",
        "max_concurrent_deletes",
        "stream_results",
        "snapshot_ids",
        "clean_expired_metadata",
    ),
    "remove_orphan_files": (
        "table",
        "older_than",
        "location",
        "dry_run",
        "max_concurrent_deletes",
        "file_list_view",
        "equal_schemes",
        "equal_authorities",
        "prefix_listing",
    ),
    "rollback_to_snapshot": ("table", "snapshot_id"),
    "rollback_to_timestamp": ("table", "timestamp"),
    "set_current_snapshot": ("table", "snapshot_id", "ref"),
    "fast_forward": ("table", "branch", "to"),
}

_CALL_RE = re.compile(r"^\s*([\w`]+(?:\s*\.\s*[\w`]+)*)\s*\((.*)\)\s*;?\s*$", re.DOTALL)
_NAMED_ARG_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=>\s*(.*?)\s*$", re.DOTALL)
_STRING_RE = re.compile(r"""^(?:'((?:[^']|'')*)'|"((?:[^"]|"")*)")$""", re.DOTALL)
_INT_RE = re.compile(r"^[+-]?\d+$")
_TS_PREFIX_RE = re.compile(r"^(?:TIMESTAMP(?:_NTZ|_LTZ)?)\s+", re.IGNORECASE)


def qualify_table_name(name: str, namespace: str) -> str:
    """Lower-case `name` and add `namespace` when it is unqualified (`orders` -> `db.orders`)."""
    cleaned = name.strip().strip('`"').lower()
    return cleaned if "." in cleaned else f"{namespace}.{cleaned}"


def parse_spark_call(text: str) -> MaintenanceAction:
    """Convert the text after `CALL` (e.g. `cat.system.expire_snapshots(...)`) to an action."""
    match = _CALL_RE.match(_strip_comments(text))
    if not match:
        raise UnsupportedCall("CALL is not of the form <procedure>(<arguments>)")
    parts = [p.strip().strip("`").lower() for p in match.group(1).split(".")]
    if len(parts) < 2 or parts[-2] != "system":
        raise UnsupportedCall(f"procedure {match.group(1).strip()!r} is not in the system schema")
    procedure = parts[-1]
    params = _PROCEDURE_PARAMS.get(procedure)
    if params is None:
        raise UnsupportedCall(f"procedure system.{procedure} is not recognised")
    args = _bind_arguments(procedure, params, _split_arguments(match.group(2)))

    table = _string_value(args.get("table"))
    if not table:
        raise UnsupportedCall(f"system.{procedure} requires a literal string `table` argument")

    if procedure == "expire_snapshots":
        return ExpireSnapshots(
            table,
            older_than=_timestamp_value(args.get("older_than")),
            retain_last=_int_arg(args, "retain_last"),
            snapshot_ids=_int_list_arg(args, "snapshot_ids"),
        )
    if procedure == "remove_orphan_files":
        return RemoveOrphanFiles(table, older_than=_timestamp_value(args.get("older_than")))
    if procedure == "rollback_to_snapshot":
        snapshot_id = _int_arg(args, "snapshot_id")
        if snapshot_id is None:
            raise UnsupportedCall("rollback_to_snapshot requires a literal snapshot_id")
        return RollbackToSnapshot(table, snapshot_id=snapshot_id)
    if procedure == "rollback_to_timestamp":
        as_of = _timestamp_value(args.get("timestamp"))
        if as_of is None:
            raise UnsupportedCall("rollback_to_timestamp requires a literal timestamp")
        return RollbackToSnapshot(table, as_of=as_of)
    if procedure == "set_current_snapshot":
        snapshot_id, ref = _int_arg(args, "snapshot_id"), _string_value(args.get("ref"))
        if (snapshot_id is None) == (ref is None):
            raise UnsupportedCall("set_current_snapshot needs exactly one of snapshot_id or ref")
        return RollbackToSnapshot(table, snapshot_id=snapshot_id, ref=ref)
    branch, to = _string_value(args.get("branch")), _string_value(args.get("to"))
    if not branch or not to:
        raise UnsupportedCall("fast_forward requires literal branch and to arguments")
    return FastForward(table, branch=branch, to=to)


def _strip_comments(text: str) -> str:
    """Replace `-- ...` and `/* ... */` comments outside quotes with a space."""
    out: list[str] = []
    i, quote = 0, ""
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
            out.append(ch)
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            out.append(" ")
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise UnsupportedCall("unterminated block comment")
            i = end + 2
            out.append(" ")
            continue
        else:
            out.append(ch)
        i += 1
    if quote:
        raise UnsupportedCall("unterminated quoted string")
    return "".join(out)


def _split_arguments(text: str) -> list[str]:
    """Split on top-level commas, respecting quotes and (), [] nesting."""
    args: list[str] = []
    depth, quote, start = 0, "", 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth < 0:
                raise UnsupportedCall("unbalanced parentheses in CALL arguments")
        elif ch == "," and depth == 0:
            args.append(text[start:i])
            start = i + 1
    if depth != 0 or quote:
        raise UnsupportedCall("unbalanced parentheses or quotes in CALL arguments")
    tail = text[start:]
    if tail.strip() or args:
        args.append(tail)
    if any(not a.strip() for a in args):
        raise UnsupportedCall("empty CALL argument")
    return args


def _bind_arguments(procedure: str, params: tuple[str, ...], raw: list[str]) -> dict[str, str]:
    named = [_NAMED_ARG_RE.match(a) for a in raw]
    bound: dict[str, str] = {}
    if raw and all(named):
        for m in named:
            assert m is not None
            name = m.group(1).lower()
            if name not in params:
                raise UnsupportedCall(f"system.{procedure} has no argument {name!r}")
            if name in bound:
                raise UnsupportedCall(f"argument {name!r} given twice")
            bound[name] = m.group(2)
    elif not any(named):
        if len(raw) > len(params):
            raise UnsupportedCall(f"too many arguments for system.{procedure}")
        bound = {name: value.strip() for name, value in zip(params, raw, strict=False)}
    else:
        raise UnsupportedCall("positional and named arguments cannot be mixed")
    return bound


def _string_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    match = _STRING_RE.match(raw.strip())
    if not match:
        return None
    single, double = match.group(1), match.group(2)
    return single.replace("''", "'") if single is not None else double.replace('""', '"')


def _int_arg(args: dict[str, str], name: str) -> int | None:
    raw = args.get(name)
    if raw is None:
        return None
    if not _INT_RE.match(raw.strip()):
        raise UnsupportedCall(f"argument {name!r} must be an integer literal")
    return int(raw)


def _int_list_arg(args: dict[str, str], name: str) -> tuple[int, ...]:
    raw = args.get(name)
    if raw is None:
        return ()
    match = re.fullmatch(r"(?is)\s*array\s*[(\[](.*)[)\]]\s*", raw)
    items = [i.strip() for i in match.group(1).split(",")] if match else []
    if not match or not items or not all(_INT_RE.match(i) for i in items):
        raise UnsupportedCall(f"argument {name!r} must be array(<integer literals>)")
    return tuple(int(i) for i in items)


def _timestamp_value(raw: str | None) -> datetime | None:
    """Parse `TIMESTAMP '2026-01-01 00:00:00'` or `'2026-01-01T00:00:00Z'`; None if not literal."""
    if raw is None:
        return None
    literal = _string_value(_TS_PREFIX_RE.sub("", raw.strip()))
    if literal is None:
        return None
    try:
        return datetime.fromisoformat(literal.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
