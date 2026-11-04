"""Check one dbt training model: inline its upstream models, build the spec, validate.

The result is a `LeakageReport` of the inlined query (so the JSON matches `sqlguard leakage
check`), plus the dbt model each finding and each suggested fix belongs to.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Finding, Severity
from sqlguard.dbt.attribution import Attribution, Attributor
from sqlguard.dbt.inline import inline
from sqlguard.dbt.manifest import Manifest, ManifestError, Node
from sqlguard.dbt.spec import build_spec
from sqlguard.leakage.analysis import analyze_tree
from sqlguard.leakage.fixes import Fix, suggest_fix
from sqlguard.leakage.replay import Replayer
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import LeakageReport, Weights, validate

TRAINING_ROLE = "training_set"


@dataclass
class ModelCheck:
    node: Node
    sql: str
    report: LeakageReport
    spec: LeakageSpec | None = None
    warnings: list[str] = field(default_factory=list)
    finding_models: list[Attribution] = field(default_factory=list)  # parallel to report.findings
    fixes: list[Fix] = field(default_factory=list)
    fix_models: list[Attribution] = field(default_factory=list)  # parallel to fixes
    suggested: bool = False  # fixes were asked for (an empty list is then an answer)
    models: list[Node] = field(default_factory=list)  # models whose SQL was checked
    boundaries: list[Node] = field(default_factory=list)  # physical tables the query reads

    @property
    def trusted(self) -> list[Node]:
        """Models read as declared (temporal, static, spine): their own SQL is not checked."""
        return [n for n in self.boundaries if n.kind == "model"]

    @property
    def fix_files(self) -> list[str]:
        """Per suggested fix, the files of the models it belongs to."""
        return [_files(a) for a in self.fix_models]

    @property
    def diff_file(self) -> str:
        """The file the suggested diff is labelled with: the model of the first rewrite when it
        is a single one, else the training model."""
        for fix, found in zip(self.fixes, self.fix_models, strict=True):
            if fix.rewrites:
                return (found.models[0] if len(found.models) == 1 else self.node).original_file_path
        return self.node.original_file_path

    def to_dict(self) -> dict[str, Any]:
        """The entry `sqlguard leakage check --json` prints, with the models added."""
        entry: dict[str, Any] = {
            "file": self.node.original_file_path,
            "node": self.node.unique_id,
            **self.report.to_dict(),
        }
        entry["findings"] = [
            {**finding.to_dict(), **_attribution(found)}
            for finding, found in zip(self.report.findings, self.finding_models, strict=True)
        ]
        if self.suggested:
            entry["fixes"] = [
                {**fix.to_dict(), **_attribution(found)}
                for fix, found in zip(self.fixes, self.fix_models, strict=True)
            ]
        entry["trusted_models"] = [
            {"node": m.unique_id, "file": m.original_file_path, "declared": _declared(m)}
            for m in self.trusted
        ]
        if self.warnings:
            entry["warnings"] = self.warnings
        return entry


def _declared(node: Node) -> str:
    return node.role or "time_column"


def _files(found: Attribution) -> str:
    files = ", ".join(m.original_file_path for m in found.models)
    return files if found.certain else f"one of: {files}"


def _attribution(found: Attribution) -> dict[str, Any]:
    return {
        "models": [{"node": m.unique_id, "file": m.original_file_path} for m in found.models],
        "attribution": "model" if found.certain else "candidates",
    }


def training_sets(manifest: Manifest, select: list[str] | None = None) -> list[Node]:
    """The `role: training_set` models, narrowed to those matching `select`.

    A pattern is a model name, a unique_id or a file path, and may use `*` and `?`. Models are
    not selected by dbt state: static analysis is cheap, and a meta-only change must be re-checked.
    """
    found = sorted(manifest.with_role(TRAINING_ROLE), key=lambda n: n.unique_id)
    if not found:
        raise ManifestError(
            f"{manifest.path}: no model declares meta.leakage.role: {TRAINING_ROLE}"
        )
    if not select:
        return found
    chosen = [
        n
        for n in found
        if any(
            fnmatch.fnmatchcase(value, pattern)
            for pattern in select
            for value in (n.name, n.unique_id, n.original_file_path)
        )
    ]
    if not chosen:
        known = ", ".join(n.name for n in found)
        raise ManifestError(f"--select {' '.join(select)!r} matches no {TRAINING_ROLE}: {known}")
    return chosen


def check_model(
    manifest: Manifest,
    root: Node,
    *,
    override: dict[str, Any] | None = None,
    column_types: dict[str, dict[str, str]] | None = None,
    catalog: LakehouseCatalog | None = None,
    replay_for: Callable[[LeakageSpec], Replayer] | None = None,
    weights: Weights | None = None,
    suggest: bool = False,
) -> ModelCheck:
    inlined = inline(manifest, root)
    built = build_spec(inlined, manifest.dialect, override, column_types)
    if built.gaps:  # the analysis cannot qualify the query: say why, never guess
        report = LeakageReport(
            "unverifiable", 1.0, [Finding("SPEC", Severity.LOW, gap) for gap in built.gaps]
        )
        return ModelCheck(
            root,
            inlined.sql,
            report,
            built.spec,
            built.warnings,
            [Attribution([root], True) for _ in report.findings],
            models=list(inlined.models.values()),
            boundaries=list(inlined.boundaries.values()),
        )

    replay = replay_for(built.spec) if replay_for else False
    weights = weights or Weights.load()
    report = validate(
        inlined.sql,
        built.spec,
        schema=built.schema,
        catalog=catalog,
        replay=replay,
        weights=weights,
    )
    try:
        attributor = Attributor(inlined, analyze_tree(inlined.tree, built.spec, built.schema))
    except Exception:  # noqa: BLE001 - the report already says the query could not be analyzed
        attributor = None

    def models(finding: Finding) -> Attribution:
        return attributor.models(finding) if attributor else Attribution([root], False)

    result = ModelCheck(
        root,
        inlined.sql,
        report,
        built.spec,
        built.warnings,
        [models(f) for f in report.findings],
        models=list(inlined.models.values()),
        boundaries=list(inlined.boundaries.values()),
    )
    if suggest:
        result.suggested = True
        result.fixes = suggest_fix(
            inlined.sql,
            built.spec,
            report,
            schema=built.schema,
            catalog=catalog,
            replay=replay,
            weights=weights,
        )
        result.fix_models = [
            models(Finding(fix.rule, Severity.HIGH, "", fix.location)) for fix in result.fixes
        ]
    return result


def stale_files(manifest_path: Path | str, checks: list[ModelCheck]) -> list[str]:
    """Files of the checked models and tables that were edited after the manifest was written.

    The check reads the manifest, not the project: a hook that does not compile first would
    judge the SQL as it was at the last `dbt compile`. Paths are looked up from the working
    directory and from the project that holds the manifest's directory.
    """
    manifest_file = Path(manifest_path).resolve()
    written = manifest_file.stat().st_mtime
    bases = [Path.cwd(), manifest_file.parent.parent]
    nodes = {n.unique_id: n for c in checks for n in (*c.models, *c.boundaries)}
    stale = []
    for node in nodes.values():
        for base in bases:
            file = base / node.original_file_path
            if file.is_file():
                if file.stat().st_mtime > written:
                    stale.append(node.original_file_path)
                break
    return sorted(set(stale))
