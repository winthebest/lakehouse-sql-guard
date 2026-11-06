"""Pure parts of the real-world runner: annotate a manifest, build the catalog, make table rows.

Nothing here runs dbt, git or the checker, so it is unit-tested directly.

A project declares nothing for the checker, and its files are never edited. The spec file's
`realworld:` block says what a user would have written as `meta.leakage` in the project's yml
(which models are training sets, which models are read as label spine / static / temporal
tables) and which columns and types the project's own docs give for those tables. The runner
applies the first to a copy of the compiled manifest and writes the second as the `catalog.json`
that `dbt docs generate` would have made. The spec file itself is passed to `sqlguard` unchanged
(it ignores the `realworld:` block).
"""

from __future__ import annotations

import copy
import csv
import json
import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

FROZEN_TAG = "rules-frozen-v3"
WITHHELD = "(withheld: the project has no license)"
ADJUDICATION_COLUMNS = ("controller", "reviewer", "final", "reason")
FINDING_COLUMNS = (
    "id",
    "project",
    "group",
    "training_model",
    "rule",
    "severity",
    "message",
    "location",
    "model_file",
    "attribution",
    "model_score",
    "model_verdict",
    "evidence",
    "compiled_sql_excerpt",
    *ADJUDICATION_COLUMNS,
)
MODEL_COLUMNS = (
    "project",
    "group",
    "training_model",
    "verdict",
    "score",
    "leak_types",
    "findings",
    "unchecked",
    "seconds",
    *ADJUDICATION_COLUMNS,
)
UNCHECKED_RULES = frozenset({"PARSE", "UNCLASSIFIED", "SPEC", "LINEAGE"})
EXCERPT_LINES = 40


class RealworldError(ValueError):
    """The spec's `realworld:` block or the manifest does not fit together."""


def resolve_node(manifest: dict[str, Any], ref: str) -> str:
    """The unique_id a `kind:name` reference stands for.

    `model:stg_users`, `seed:raw_orders`, `snapshot:snap_users` and `source:raw.users`
    (source name, then table name). The package name is not part of the reference.
    """
    kind, _, name = ref.partition(":")
    if not name:
        raise RealworldError(f"node reference {ref!r} must look like kind:name")
    pools = (manifest.get("sources", {}),) if kind == "source" else (manifest.get("nodes", {}),)
    found = []
    for pool in pools:
        for unique_id in pool:
            parts = unique_id.split(".")
            if parts[0] != kind:
                continue
            tail = ".".join(parts[2:])  # drop `<kind>.<package>.`
            if tail == name:
                found.append(unique_id)
    if len(found) != 1:
        raise RealworldError(f"node reference {ref!r} matches {len(found)} nodes: {found}")
    return found[0]


def _set_meta(node: dict[str, Any], leakage: dict[str, Any]) -> None:
    config = node.setdefault("config", {})
    meta = config.setdefault("meta", {})
    meta["leakage"] = {**meta.get("leakage", {}), **leakage}


def annotate_manifest(manifest: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    """A copy of `manifest` with the `meta.leakage` the spec's `realworld:` block declares."""
    annotated = copy.deepcopy(manifest)
    for name in block.get("training_sets") or []:
        unique_id = resolve_node(annotated, f"model:{name}")
        _set_meta(annotated["nodes"][unique_id], {"role": "training_set"})
    for ref, leakage in (block.get("meta") or {}).items():
        unique_id = resolve_node(annotated, ref)
        pool = annotated["sources"] if ref.startswith("source:") else annotated["nodes"]
        _set_meta(pool[unique_id], dict(leakage))
    return annotated


def catalog_from_schema(
    manifest: dict[str, Any], schema: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """`catalog.json` as `dbt docs generate` writes it, from `kind:name -> {column: type}`."""
    catalog: dict[str, Any] = {"nodes": {}, "sources": {}}
    for ref, columns in schema.items():
        unique_id = resolve_node(manifest, ref)
        section = "sources" if ref.startswith("source:") else "nodes"
        catalog[section][unique_id] = {
            "columns": {
                str(name): {"name": str(name), "type": str(sql_type)}
                for name, sql_type in columns.items()
            }
        }
    return catalog


def upstream_models(manifest: dict[str, Any], root: str, stop_at: Iterable[str] = ()) -> list[str]:
    """Models whose SQL is part of the checked query of `root` (itself included).

    Walks `depends_on` through models only, and stops at the models in `stop_at` (read as physical
    tables, so their SQL is not part of the query). Sources, seeds and snapshots are tables.
    """
    stop = set(stop_at)
    nodes = manifest["nodes"]
    seen: dict[str, None] = {root: None}
    todo = [root]
    while todo:
        current = todo.pop()
        for dep in nodes[current].get("depends_on", {}).get("nodes", []):
            node = nodes.get(dep)
            if node is None or node["resource_type"] != "model" or dep in stop or dep in seen:
                continue
            seen[dep] = None
            todo.append(dep)
    return list(seen)


def boundary_models(manifest: dict[str, Any]) -> list[str]:
    """Models the checker reads as physical tables (spine, static or a time_column)."""
    found = []
    for unique_id, node in manifest["nodes"].items():
        leakage = (node.get("config", {}).get("meta") or {}).get("leakage") or {}
        if node["resource_type"] == "model" and (
            leakage.get("role") in ("label_spine", "static") or "time_column" in leakage
        ):
            found.append(unique_id)
    return found


def _squash(text: str) -> str:
    return " ".join(text.lower().replace('"', "").split())


def needles(finding: dict[str, Any]) -> list[str]:
    """Strings that locate a finding's scope in the compiled SQL (lowercase, whitespace squashed).

    A window finding carries the window's SQL: its frame clause (`range between ... following`)
    is what is searched for. Other findings give their table reference, column or expression.
    """
    found: list[str] = []
    evidence = finding.get("evidence") or {}
    window = evidence.get("window")
    if isinstance(window, str):
        frame = re.search(r"(?i)\b(range|rows|groups)\s+between\b.*", window)
        found.append(_squash(frame.group(0).rstrip(" )") if frame else window))
    for key in ("expression", "predicate"):
        if isinstance(evidence.get(key), str):
            found.append(_squash(evidence[key]))
    for predicate in evidence.get("predicates") or []:
        found.append(_squash(str(predicate)))
    location = str(finding.get("location") or "")
    if location:
        found += [_squash(location.split(" AS ")[0]), _squash(location)]
    return [n for n in dict.fromkeys(found) if n]


def _hit(lines: list[str], wanted: list[str]) -> int | None:
    squashed = [_squash(line) for line in lines]
    for needle in wanted:
        for index, line in enumerate(squashed):
            if needle in line:
                return index
    return None


def excerpt(compiled_code: str, wanted: list[str], max_lines: int = EXCERPT_LINES) -> str:
    """At most `max_lines` lines of the compiled SQL around the first line matching `wanted`.

    Without a match the excerpt starts at the top. It is cut on line boundaries.
    """
    lines = compiled_code.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    hit = _hit(lines, wanted) or 0
    start = max(0, min(hit - max_lines // 4, len(lines) - max_lines))
    return "\n".join(lines[start : start + max_lines])


def scope_excerpt(
    manifest: dict[str, Any], preferred: list[dict[str, str]], root: str, finding: dict[str, Any]
) -> str:
    """The compiled SQL of the flagged scope, with the file it comes from.

    The attributed models are tried first, then every other model of the project: the checker
    names the model that holds a leak when it can, and lists candidates when it cannot, so the
    text that matches the finding is looked up in the compiled SQL itself.
    """
    wanted = needles(finding)
    order = [m["node"] for m in preferred] + [root] + list(manifest["nodes"])
    models = []
    for unique_id in dict.fromkeys(order):
        node = manifest["nodes"].get(unique_id)
        if node is not None and node["resource_type"] == "model" and node.get("compiled_code"):
            models.append(node)
    for needle in wanted:  # the most specific needle first, over every model
        for node in models:
            if _hit(node["compiled_code"].strip().splitlines(), [needle]) is not None:
                return (
                    f"-- {node['original_file_path']}\n{excerpt(node['compiled_code'], [needle])}"
                )
    if not models:
        return ""
    first = models[0]
    path = first["original_file_path"]
    return f"-- {path} (no line matches the finding)\n{excerpt(first['compiled_code'], wanted)}"


def _compact(evidence: dict[str, Any]) -> str:
    return (
        json.dumps(evidence, sort_keys=True, default=str, separators=(",", ":")) if evidence else ""
    )


def finding_rows(
    project: dict[str, Any], entries: list[dict[str, Any]], manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(leak findings, findings that say part of a query was not checked) from `--json` entries.

    `project` carries `repo`, `group` and `publish`. For a project without a license the code
    columns (evidence, compiled SQL excerpt) are withheld in the published view; the full view
    keeps them. Rows are returned as full views; `public_row` produces the published one.
    """
    leaks: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    for entry in entries:
        for number, finding in enumerate(entry["findings"], 1):
            models = finding.get("models") or []
            files = ", ".join(m["file"] for m in models)
            code = scope_excerpt(manifest, models, entry["node"], finding)
            row = {
                "id": f"{project['repo']}#{entry['node'].rsplit('.', 1)[-1]}#{number}",
                "project": project["repo"],
                "group": project["group"],
                "training_model": entry["node"].rsplit(".", 1)[-1],
                "rule": finding["rule_id"],
                "severity": finding["severity"],
                "message": finding["message"],
                "location": finding.get("location", ""),
                "model_file": files,
                "attribution": finding.get("attribution", ""),
                "model_score": entry["score"],
                "model_verdict": entry["verdict"],
                "evidence": _compact(finding.get("evidence") or {}),
                "compiled_sql_excerpt": code,
                **{column: "" for column in ADJUDICATION_COLUMNS},
            }
            (unchecked if finding["rule_id"] in UNCHECKED_RULES else leaks).append(row)
    return leaks, unchecked


def model_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per checked training model, for judging the verdict itself (clean included)."""
    found = []
    for record in records:
        for model in record.get("models", []):
            found.append(
                {
                    "project": record["repo"],
                    "group": record["group"],
                    "training_model": model["model"],
                    "verdict": model["verdict"],
                    "score": model.get("score", ""),
                    "leak_types": " ".join(model.get("leak_types", [])),
                    "findings": model["findings"],
                    "unchecked": model["unchecked"],
                    "seconds": model["seconds"],
                    **{column: "" for column in ADJUDICATION_COLUMNS},
                }
            )
    return found


def public_row(row: dict[str, Any], publish: str) -> dict[str, Any]:
    """The row as committed: no code of a project that has no license."""
    if publish == "code":
        return dict(row)
    return {
        **row,
        "evidence": WITHHELD if row["evidence"] else "",
        "compiled_sql_excerpt": WITHHELD,
    }


def write_csv(
    path: Path | str, rows: Iterable[dict[str, Any]], columns: Iterable[str] = FINDING_COLUMNS
) -> int:
    """Write `rows` with exactly `columns` (missing cells empty); returns the row count."""
    columns = list(columns)
    count = 0
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="raise", restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def assert_rules_frozen(
    repo: Path | str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    tag: str = FROZEN_TAG,
) -> None:
    """Fail unless `src/sqlguard` is identical to the frozen tag, committed and in the work tree."""
    done = run(
        ["git", "diff", "--quiet", tag, "HEAD", "--", "src/sqlguard"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if done.returncode == 1:
        raise RealworldError(f"src/sqlguard differs from tag {tag}: the rules are not frozen")
    if done.returncode != 0:
        raise RealworldError(f"cannot compare src/sqlguard with tag {tag}: {done.stderr.strip()}")
    pending = run(
        ["git", "status", "--porcelain", "--", "src/sqlguard"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if pending.returncode != 0 or pending.stdout.strip():
        raise RealworldError("src/sqlguard has uncommitted changes: the rules are not frozen")
