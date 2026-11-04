"""Entry point: validate(sql, spec, ...) -> LeakageReport."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Finding, Severity
from sqlguard.core.lineage import LineageError, Schema
from sqlguard.core.parsing import SqlParseError
from sqlguard.leakage.analysis import analyze
from sqlguard.leakage.replay import Replayer, ReplayResult, ReplayStatus
from sqlguard.leakage.rules import ALL_RULES
from sqlguard.leakage.spec import LeakageSpec

DEFAULT_WEIGHTS = Path(__file__).resolve().parents[3] / "config" / "leakage_weights.yaml"
_BUILTIN_WEIGHTS = {
    "rules": {"L1": 0.95, "L2": 0.9, "L3": 0.9, "L4": 0.5, "L5": 0.85, "L6": 0.95},
    "replay_mismatch": 0.9,
    "threshold": 0.5,
}


@dataclass(frozen=True)
class Weights:
    rules: dict[str, float]
    replay_mismatch: float
    threshold: float

    @classmethod
    def load(cls, path: Path | str | None = None) -> Weights:
        source = Path(path) if path else DEFAULT_WEIGHTS
        data = _BUILTIN_WEIGHTS
        if source.exists():
            with open(source, encoding="utf-8") as handle:
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
    try:
        analysis = analyze(sql, spec, schema)
    except (SqlParseError, LineageError) as err:
        return [Finding("PARSE", Severity.HIGH, f"cannot analyze query: {err}")]
    findings = [
        Finding(
            "UNCLASSIFIED",
            Severity.LOW,
            f"table {ref.table!r} is not declared in the spec; its time semantics are unknown",
            ref.describe(),
        )
        for ref in analysis.unclassified_refs
    ]
    for rule in ALL_RULES:
        findings.extend(rule.check(analysis))
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
    snapshot history, the query is also replayed point-in-time; expired history makes the
    result `unverifiable` rather than clean.
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
    leak_types = sorted(
        {f.rule_id for f in findings if f.rule_id[:1] == "L" and f.rule_id[1:].isdigit()}
    )
    if score >= weights.threshold:
        verdict = "leaky"
    elif replay_result and replay_result.status is ReplayStatus.UNVERIFIABLE:
        verdict = "unverifiable"
    else:
        verdict = "clean"
    return LeakageReport(verdict, score, findings, replay_result, leak_types)
