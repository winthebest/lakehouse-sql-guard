"""Entry point: validate(sql, spec, ...) -> LeakageReport.

Verdicts: `leaky` (score >= threshold), `unverifiable` (the guard could not check part of
the query: unparseable or non-SELECT input, undeclared tables, spec/schema mismatch,
lineage failure, expired snapshot history, or too little replay evidence), otherwise `clean`.
Never `clean` by default.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Finding, Severity
from sqlguard.core.lineage import Schema
from sqlguard.core.parsing import parse_single
from sqlguard.leakage.analysis import analyze
from sqlguard.leakage.replay import Replayer, ReplayResult, ReplayStatus
from sqlguard.leakage.rules import ALL_RULES
from sqlguard.leakage.spec import LeakageSpec

DEFAULT_WEIGHTS = Path(__file__).with_name("weights.yaml")
# Findings that mean "part of the query was not checked" rather than "leak found".
UNCHECKED = {"PARSE", "UNCLASSIFIED", "SPEC", "LINEAGE"}


@dataclass(frozen=True)
class Weights:
    rules: dict[str, float]
    replay_mismatch: float
    threshold: float

    @classmethod
    def load(cls, path: Path | str | None = None) -> Weights:
        source = Path(path) if path else DEFAULT_WEIGHTS
        with open(source, encoding="utf-8") as handle:  # a missing explicit path is an error
            data = yaml.safe_load(handle)
        return cls(dict(data["rules"]), float(data["replay_mismatch"]), float(data["threshold"]))


@dataclass(frozen=True)
class LeakageReport:
    verdict: str  # clean | leaky | unverifiable
    score: float
    findings: list[Finding]
    replay: ReplayResult | None = None
    leak_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": round(self.score, 4),
            "leak_types": self.leak_types,
            "findings": [f.to_dict() for f in self.findings],
            "replay": asdict(self.replay) if self.replay else None,
        }


def static_findings(sql: str, spec: LeakageSpec, schema: Schema) -> list[Finding]:
    """Run every static rule; any failure to analyze becomes a PARSE finding (fail closed)."""
    try:
        ast = parse_single(sql, spec.dialect)
        if not isinstance(ast, exp.Query):
            return [
                Finding(
                    "PARSE",
                    Severity.HIGH,
                    f"only SELECT queries can be validated, got {type(ast).__name__}",
                )
            ]
        analysis = analyze(sql, spec, schema)
        findings = [
            Finding(
                "UNCLASSIFIED",
                Severity.LOW,
                f"table {ref.table!r} is not declared in the spec; its time semantics are unknown",
                ref.describe(),
            )
            for ref in analysis.unclassified_refs
        ]
        findings += [Finding("SPEC", Severity.LOW, message) for message in analysis.spec_errors]
        for rule in ALL_RULES:
            findings.extend(rule.check(analysis))
    except Exception as err:  # noqa: BLE001 - any analysis failure must not read as clean
        return [Finding("PARSE", Severity.HIGH, f"cannot analyze query: {err}")]
    return findings


def _score(findings: list[Finding], replay: ReplayResult | None, weights: Weights) -> float:
    keep = 1.0
    for finding in findings:
        keep *= 1 - weights.rules.get(finding.rule_id, 0.0)
    if replay and replay.status is ReplayStatus.MISMATCH:
        keep *= 1 - weights.replay_mismatch
    return 1 - keep


def validate(
    sql: str,
    spec: LeakageSpec,
    *,
    schema: Schema | None = None,
    catalog: LakehouseCatalog | None = None,
    replay: bool | Replayer = False,
    weights: Weights | None = None,
) -> LeakageReport:
    """Check a training-set query for temporal leakage.

    Static rules always run. With `replay=True` (or a shared Replayer) and a catalog with
    snapshot history, the query is also replayed point-in-time.
    """
    weights = weights or Weights.load()
    if schema is None:
        if catalog is None:
            raise ValueError("validate() needs a schema or a catalog")
        schema = catalog.schema_for()
    findings = static_findings(sql, spec, schema)
    if any(f.rule_id == "PARSE" for f in findings):
        return LeakageReport("unverifiable", 1.0, findings)

    replay_result = None
    if replay:
        replayer = replay if isinstance(replay, Replayer) else None
        if replayer is None:
            if catalog is None:
                raise ValueError("replay needs a catalog with snapshot history")
            replayer = Replayer(catalog, spec)
        replay_result = replayer.run(sql)
        if replay_result.status is ReplayStatus.MISMATCH:
            findings.append(
                Finding(
                    "REPLAY",
                    Severity.HIGH,
                    "feature values differ from what was knowable at prediction time",
                    evidence={
                        "rows_mismatched": replay_result.rows_mismatched,
                        "rows_compared": replay_result.rows_compared,
                        "columns": replay_result.mismatched_columns,
                    },
                )
            )

    score = _score([f for f in findings if f.rule_id != "REPLAY"], replay_result, weights)
    leak_types = sorted({f.rule_id for f in findings if f.rule_id in weights.rules})
    unchecked = (
        any(f.rule_id in UNCHECKED for f in findings)
        or (replay_result is not None and replay_result.status is ReplayStatus.UNVERIFIABLE)
        # an L4 hint is only settled by replay
        or (replay_result is None and any(f.rule_id == "L4" for f in findings))
    )
    if score >= weights.threshold:
        verdict = "leaky"
    elif unchecked:
        verdict = "unverifiable"
    else:
        verdict = "clean"
    return LeakageReport(verdict, score, findings, replay_result, leak_types)
