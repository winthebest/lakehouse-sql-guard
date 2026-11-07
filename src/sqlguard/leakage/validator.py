"""Entry point: validate(sql, spec, ...) -> LeakageReport.

Verdicts: `leaky` (score >= threshold), `unverifiable` (the guard could not check part of
the query: unparseable or non-SELECT input, undeclared tables, spec/schema mismatch,
lineage failure, expired snapshot history, or too little replay evidence for a query with a
static ingestion-time hint), otherwise `clean`.
Never `clean` by default.

A static finding that provably feeds only output columns the spec declares as label or metadata
(and decides no row) is kept in the report as INFO with weight 0 and `evidence.non_feature_only`;
it is not a leak type and does not move the score (see `roles`).

A replay result never lowers a static score. When replay found no difference, the findings about
the tables it hid later rows of carry that fact as evidence (`replay_no_mismatch`, with the
exposure counts), so a reader can weigh it, but the verdict is the static one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Finding, Severity
from sqlguard.core.lineage import Schema
from sqlguard.core.parsing import parse_single
from sqlguard.leakage.analysis import QueryAnalysis, analyze
from sqlguard.leakage.replay import MIN_POINTS, MIN_ROWS, Replayer, ReplayResult, ReplayStatus
from sqlguard.leakage.rules import ALL_RULES
from sqlguard.leakage.spec import LeakageSpec

DEFAULT_WEIGHTS = Path(__file__).with_name("weights.yaml")
# Findings that mean "part of the query was not checked" rather than "leak found".
UNCHECKED = {"PARSE", "UNCLASSIFIED", "SPEC", "LINEAGE"}
# Static rules about which rows a feature reads; replay can say whether the rows it hid changed
# anything (see `annotate`), for these only.
ROW_READ_RULES = frozenset({"L1", "L2", "L3", "L5"})


@dataclass(frozen=True)
class Weights:
    rules: dict[str, float]
    replay_mismatch: float
    threshold: float
    # an L4 finding for an event-time bound shorter than the spec's declared max_ingest_delay
    l4_declared_delay: float = 0.9

    @classmethod
    def load(cls, path: Path | str | None = None) -> Weights:
        source = Path(path) if path else DEFAULT_WEIGHTS
        with open(source, encoding="utf-8") as handle:  # a missing explicit path is an error
            data = yaml.safe_load(handle)
        return cls(
            dict(data["rules"]),
            float(data["replay_mismatch"]),
            float(data["threshold"]),
            float(data.get("l4_declared_delay", cls.l4_declared_delay)),
        )

    def weight(self, finding: Finding) -> float:
        if finding.evidence.get("non_feature_only"):  # shown for information, not scored
            return 0.0
        if finding.rule_id == "L4" and finding.evidence.get("exceeds_declared_delay"):
            return self.l4_declared_delay
        return self.rules.get(finding.rule_id, 0.0)


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


def annotate(findings: list[Finding], replay: ReplayResult | None) -> list[Finding]:
    """Attach what a conclusive replay PASS showed to the findings it is about; score unchanged.

    For an L1/L2/L3/L5 finding on a table, the evidence says how many later rows of that table
    replay hid from the replayed query and at how many prediction times, and whether that was
    enough to say anything: a pass over no hidden rows says nothing about the table. This is
    information for a reader. It does not change a score or a verdict, because a pass over a
    sample of prediction times cannot rule out a leak that only some rows show.
    """
    if replay is None or replay.status is not ReplayStatus.PASS or replay.insufficient_evidence:
        return findings
    annotated = []
    for f in findings:
        table = f.evidence.get("table")
        if f.rule_id not in ROW_READ_RULES or not isinstance(table, str):
            annotated.append(f)
            continue
        rows, points = replay.exposure.get(table, 0), replay.exposed_points.get(table, 0)
        informative = rows >= MIN_ROWS and points >= MIN_POINTS
        note = (
            f"replay found no mismatch over {replay.timestamps_compared} prediction times, with "
            f"{rows} later rows of {table} hidden at {points} of them"
            + ("" if informative else " (too few to say anything about it)")
            + "; this does not change the score"
        )
        annotated.append(
            replace(
                f,
                message=f"{f.message} ({note})",
                evidence={
                    **f.evidence,
                    "replay_no_mismatch": True,
                    "replay_exposed_rows": rows,
                    "replay_exposed_points": points,
                    "replay_informative": informative,
                },
            )
        )
    return annotated


def analysis_findings(analysis: QueryAnalysis) -> list[Finding]:
    """Every static finding of an analyzed query (the checks `static_findings` runs)."""
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
    return findings


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
        findings = analysis_findings(analyze(sql, spec, schema))
    except Exception as err:  # noqa: BLE001 - any analysis failure must not read as clean
        return [Finding("PARSE", Severity.HIGH, f"cannot analyze query: {err}")]
    return findings


def _score(findings: list[Finding], replay: ReplayResult | None, weights: Weights) -> float:
    keep = 1.0
    for finding in findings:
        keep *= 1 - weights.weight(finding)
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

    findings = annotate(findings, replay_result)
    score = _score([f for f in findings if f.rule_id != "REPLAY"], replay_result, weights)
    leak_types = sorted(
        {
            f.rule_id
            for f in findings
            if f.rule_id in weights.rules and not f.evidence.get("non_feature_only")
        }
    )
    needs_replay = any(f.rule_id == "L4" for f in findings)  # only replay can settle the hint
    replay_blocks = (
        replay_result is not None
        and replay_result.status is ReplayStatus.UNVERIFIABLE
        # too little evidence only matters when something static is waiting on it; the reason
        # stays in the report's replay field either way
        and (needs_replay or not replay_result.insufficient_evidence)
    )
    unchecked = (
        any(f.rule_id in UNCHECKED for f in findings)
        or replay_blocks
        or (replay_result is None and needs_replay)
    )
    if score >= weights.threshold:
        verdict = "leaky"
    elif unchecked:
        verdict = "unverifiable"
    else:
        verdict = "clean"
    return LeakageReport(verdict, score, findings, replay_result, leak_types)
