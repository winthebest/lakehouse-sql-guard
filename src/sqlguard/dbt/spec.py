"""Build a LeakageSpec, and the schema the analysis needs, for one training model.

The settings come from `meta.leakage` of the nodes the inlined query reads:

- the training model (`role: training_set`) and the label spine (`role: label_spine`) give
  `entity_keys`, `prediction_ts`, `label_columns` (the training model wins, then the spine) and
  the optional `boundary` and `label_known_after`;
- a physical table declares what it is: `role: static`, `time_column` (+ `availability_column`,
  `max_ingest_delay`) for a temporal table, `outcome_columns` for facts known after the outcome.

Tables are named as the catalog names them: a relation `"db"."raw"."orders"` is the table `orders`
(or the table `iceberg_table` names), and the spec's `relations` maps the SQL name to it, so the
analysis and replay agree on which table a query reads. A `--spec` YAML file is merged on top and
wins, with a warning for every value it changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from sqlguard.core.lineage import Schema
from sqlguard.dbt.inline import Inlined, relation_key
from sqlguard.dbt.manifest import Node
from sqlguard.leakage.spec import LeakageSpec, SpecError
from sqlguard.leakage.spec import relation_key as spec_relation_key

UNKNOWN_TYPE = "UNKNOWN"
_SPINE_KEYS = ("entity_keys", "prediction_ts", "label_columns", "boundary", "label_known_after")
_MAPPING_KEYS = ("time_columns", "availability_columns", "max_ingest_delay", "relations")
# --spec fields that can only make a check more lenient than the dbt meta alone
_RELAXING = (
    "max_ingest_delay",
    "label_known_after",
    "availability_columns",
    "static_tables",
    "label_table",
    "relations",
)


@dataclass(frozen=True)
class SpecBuild:
    spec: LeakageSpec
    schema: Schema
    warnings: list[str] = field(default_factory=list)
    # why the query cannot be checked at all (no columns known for a table the query reads)
    gaps: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PhysicalTable:
    name: str  # the catalog table's own name, as the spec spells it
    target: str  # the catalog table (`namespace.name` or `name`) a SQL relation name stands for
    relation: str | None  # the qualified SQL name (`db.schema.name`), None for a bare name


def physical_table(node: Node, dialect: str, remap: Mapping[str, str]) -> PhysicalTable:
    """The catalog table a node's relation is.

    It is what the `--spec` file's `relations` says (`remap`), else `iceberg_table` from the
    node's meta (dbt-duckdb's iceberg plugin reads it, defaulting to `<schema>.<identifier>`),
    else `<schema>.<identifier>`.
    """
    if not node.relation_name:
        raise SpecError(f"{node.unique_id} has no relation name, so the query cannot read it")
    parts = relation_key(node.relation_name, dialect)
    schema = parts[-2] if len(parts) > 1 else ""
    template = node.meta.get("iceberg_table")
    relation = ".".join(parts) if len(parts) > 1 else None
    if relation in remap:
        target = remap[relation]
    elif isinstance(template, str) and template.strip():
        try:
            target = template.format(schema=schema, identifier=parts[-1]).strip().lower()
        except (KeyError, IndexError):
            raise SpecError(
                f"{node.unique_id}: iceberg_table {template!r} may only use {{schema}} and "
                f"{{identifier}}"
            ) from None
    else:
        target = f"{schema}.{parts[-1]}" if schema else parts[-1]
    return PhysicalTable(target.rsplit(".", 1)[-1], target, relation)


def physical_tables(
    inlined: Inlined, dialect: str, remap: Mapping[str, str] = MappingProxyType({})
) -> dict[str, PhysicalTable]:
    """unique_id -> catalog table, for every physical table the query reads."""
    tables = {n.unique_id: physical_table(n, dialect, remap) for n in inlined.boundaries.values()}
    owners: dict[str, str] = {}
    for unique_id, table in tables.items():
        other = owners.setdefault(table.name, unique_id)
        if other != unique_id:
            raise SpecError(
                f"{unique_id} and {other} are both the table {table.name!r}, and the spec names "
                "tables without their schema. Give one a different catalog table: set "
                "`iceberg_table` in its meta, or map its relation to another name under "
                "`relations` in the --spec file."
            )
    return tables


def _setting(key: str, *nodes: Node) -> Any:
    for node in nodes:
        if key in node.leakage:
            return node.leakage[key]
    return None


def derive_spec(
    inlined: Inlined, dialect: str, remap: Mapping[str, str] = MappingProxyType({})
) -> dict[str, Any]:
    """The spec fields the dbt meta of the query's tables give, as a LeakageSpec dict."""
    root = inlined.root
    boundaries = list(inlined.boundaries.values())
    spines = [n for n in boundaries if n.role == "label_spine"]
    spine = spines[0] if len(spines) == 1 else None
    data: dict[str, Any] = {"dialect": dialect}
    if len(spines) > 1:
        names = ", ".join(sorted(n.name for n in spines))
        raise SpecError(f"{root.unique_id} reads more than one label_spine ({names})")
    chain = [root, *([spine] if spine else [])]

    tables = physical_tables(inlined, dialect, remap)
    table = {unique_id: t.name for unique_id, t in tables.items()}
    relations = {t.relation: t.target for t in tables.values() if t.relation}

    if spine:
        data["label_table"] = table[spine.unique_id]
    for key in _SPINE_KEYS:
        value = _setting(key, *chain)
        if value is not None:
            data[key] = value
    times: dict[str, str] = {}
    available: dict[str, str] = {}
    delay: dict[str, Any] = {}
    static: list[str] = []
    outcomes: list[str] = []
    for node in boundaries:
        leak = node.leakage
        name = table[node.unique_id]
        if "time_column" in leak:
            times[name] = leak["time_column"]
            if "availability_column" in leak:
                available[name] = leak["availability_column"]
            if "max_ingest_delay" in leak:
                delay[name] = leak["max_ingest_delay"]
        elif "availability_column" in leak or "max_ingest_delay" in leak:
            raise SpecError(
                f"{node.unique_id}: availability_column and max_ingest_delay need a time_column"
            )
        if leak.get("role") == "static":
            static.append(name)
        columns = leak.get("outcome_columns") or []
        if not isinstance(columns, list):
            raise SpecError(f"{node.unique_id}: outcome_columns must be a list of column names")
        outcomes += [f"{name}.{column}" for column in columns]
    for key, value in (
        ("time_columns", times),
        ("availability_columns", available),
        ("max_ingest_delay", delay),
        ("relations", relations),
    ):
        if value:
            data[key] = value
    if static:
        data["static_tables"] = static
    if outcomes:
        data["outcome_columns"] = outcomes
    return data


def _same(a: Any, b: Any) -> bool:
    return str(a).strip().lower() == str(b).strip().lower()


def merge_spec(
    derived: dict[str, Any], override: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """`derived` with the fields of the `--spec` file on top, and the warnings that go with it.

    A value the file changes is warned about. So is every field of the file that can relax a rule
    (`boundary: inclusive`, `max_ingest_delay`, `label_known_after`, `availability_columns`,
    `static_tables`, `label_table`, `relations`) even when the dbt meta sets nothing there: the
    file is a deliberate loosening and the run should say so.
    """
    merged = dict(derived)
    warnings = []
    for key, value in override.items():
        if key in _MAPPING_KEYS and isinstance(value, dict) and isinstance(derived.get(key), dict):
            known = {str(k).lower(): v for k, v in derived[key].items()}
            entries = dict(derived[key])
            for entry, new in value.items():
                old = known.get(str(entry).lower())
                if old is None:
                    if key in _RELAXING:
                        warnings.append(_relaxes(f"{key}[{entry}]", new))
                elif not _same(old, new):
                    warnings.append(f"--spec changes {key}[{entry}] from dbt meta: {old} -> {new}")
                entries[entry] = new
            merged[key] = entries
            continue
        old = derived.get(key)
        if old is None:
            relaxes = key in _RELAXING or (key == "boundary" and not _same(value, "strict"))
            if relaxes and isinstance(value, dict):
                warnings += [_relaxes(f"{key}[{entry}]", new) for entry, new in value.items()]
            elif relaxes and isinstance(value, list):
                warnings += [_relaxes(f"{key}", name) for name in value]
            elif relaxes:
                warnings.append(_relaxes(key, value))
        elif old != value and not _same(old, value):
            if key in ("static_tables",) and isinstance(value, list) and isinstance(old, list):
                warnings += [
                    _relaxes(key, name)
                    for name in value
                    if str(name).lower() not in map(str.lower, old)
                ]
            warnings.append(f"--spec changes {key} from dbt meta: {old} -> {value}")
        merged[key] = value
    return merged, warnings


def _relaxes(field_name: str, value: Any) -> str:
    return f"--spec sets {field_name} = {value} (the dbt meta does not); this can relax a rule"


def build_schema(
    inlined: Inlined,
    spec: LeakageSpec,
    dialect: str,
    column_types: dict[str, dict[str, str]],
    remap: Mapping[str, str] = MappingProxyType({}),
) -> tuple[Schema, list[str]]:
    """Columns and types of the physical tables, from the manifest and `catalog.json`."""
    schema: Schema = {}
    gaps: list[str] = []
    typed = {
        (spec.label_table, spec.prediction_ts),
        *spec.time_columns.items(),
        *spec.availability_columns.items(),
    }
    tables = physical_tables(inlined, dialect, remap)
    for node in inlined.boundaries.values():
        name = tables[node.unique_id].name
        known = column_types.get(node.unique_id, {})
        columns = {c.name.lower(): c.data_type or UNKNOWN_TYPE for c in node.columns.values()}
        columns.update({c: t for c, t in known.items() if t})
        if not columns:
            gaps.append(
                f"no columns are known for {node.name} ({node.original_file_path}): declare "
                "`columns:` in its yml, or pass --catalog-json from `dbt docs generate`"
            )
            continue
        schema[name] = columns
        gaps += [
            f"the type of {name}.{column} is unknown: add `data_type` to the column in "
            f"{node.original_file_path}, or pass --catalog-json"
            for table, column in sorted(typed)
            if table == name and columns.get(column, "") == UNKNOWN_TYPE
        ]
    return schema, gaps


def build_spec(
    inlined: Inlined,
    dialect: str,
    override: dict[str, Any] | None = None,
    column_types: dict[str, dict[str, str]] | None = None,
) -> SpecBuild:
    override = override or {}
    wanted = override.get("relations") or {}
    if not isinstance(wanted, dict):
        raise SpecError(f"relations must be a mapping of relation name -> table, got {wanted!r}")
    remap = {spec_relation_key(k): spec_relation_key(v) for k, v in wanted.items()}
    derived = derive_spec(inlined, dialect, remap)
    merged, warnings = merge_spec(derived, override)
    if not merged.get("time_columns"):
        read = ", ".join(sorted(n.name for n in inlined.boundaries.values())) or "nothing"
        raise SpecError(
            f"{inlined.root.unique_id} reads no table that declares meta.leakage.time_column "
            f"(it reads: {read}); declare time_column on the temporal tables and role: static "
            "on the static ones"
        )
    try:
        spec = LeakageSpec.from_dict(merged)
    except SpecError as err:
        raise SpecError(f"{inlined.root.unique_id}: {err}") from None
    schema, gaps = build_schema(inlined, spec, dialect, column_types or {}, remap)
    return SpecBuild(spec, schema, warnings, gaps)
