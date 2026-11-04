"""Policy: turn a static classification into ALLOW / ALLOW_VIA_BRANCH / ASK / DENY with reasons.

The policy is pure data plus arithmetic. It never executes SQL; measured impact (rows affected)
arrives later through `Classification.est_impact`, filled in by the branch executor.

Invariants enforced when a policy is built, so a bad config cannot weaken the guard:
UNKNOWN and OUT_OF_SCOPE are always DENY, and IRREVERSIBLE is never auto-allowed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from sqlguard.core.catalog import DEFAULT_NAMESPACE
from sqlguard.core.findings import Decision
from sqlguard.safety.actions import (
    ExpireSnapshots,
    MaintenanceAction,
    RemoveOrphanFiles,
    qualify_table_name,
)
from sqlguard.safety.classifier import (
    ROW_MODIFYING_VERBS,
    Classification,
    Impact,
    ReversibilityClass,
    StatementClass,
)
from sqlguard.safety.invariants import InvariantConfigError, InvariantsConfig
from sqlguard.safety.store import digest

C = ReversibilityClass

DEFAULT_DECISIONS: dict[ReversibilityClass, Decision] = {
    C.READ: Decision.ALLOW,
    C.REVERSIBLE_WRITE: Decision.ALLOW_VIA_BRANCH,
    C.RISKY_WRITE: Decision.ASK,
    C.SCHEMA_BREAKING: Decision.ASK,
    C.IRREVERSIBLE: Decision.ASK,
    C.OUT_OF_SCOPE: Decision.DENY,
    C.UNKNOWN: Decision.DENY,
}

_TOP_LEVEL_KEYS = {
    "version",
    "default_namespace",
    "class_decisions",
    "protected_tables",
    "retention",
    "thresholds",
    "task_scope",
    "invariants",
    "environments",
    "require_table_key",
}
ENVIRONMENTS = frozenset({"dev", "prod"})
# statements whose result a reviewer can only follow row by row when the table has a key
_KEY_PAIRED_VERBS = frozenset({"UPDATE", "MERGE"})


class PolicyError(ValueError):
    """The policy file or mapping is invalid."""


@dataclass(frozen=True)
class TaskScope:
    """Tables an agent may touch for one task. An empty scope allows no table at all."""

    allowed_tables: frozenset[str] = frozenset()
    allowed_namespaces: frozenset[str] = frozenset()

    @classmethod
    def of(
        cls,
        tables: list[str] | tuple[str, ...] = (),
        namespaces: list[str] | tuple[str, ...] = (),
        default_namespace: str = DEFAULT_NAMESPACE,
    ) -> TaskScope:
        return cls(
            frozenset(qualify_table_name(t, default_namespace) for t in tables),
            frozenset(n.strip().lower() for n in namespaces),
        )

    def allows(self, table: str) -> bool:
        namespace = table.rsplit(".", 1)[0] if "." in table else ""
        return table in self.allowed_tables or namespace in self.allowed_namespaces


@dataclass(frozen=True)
class RetentionRule:
    """Minimum history that destructive maintenance must leave intact."""

    min_snapshot_age_days: float = 7.0
    min_snapshots: int = 1
    min_orphan_age_days: float = 3.0


@dataclass(frozen=True)
class Thresholds:
    """Blast-radius limits applied once a dry run has measured the impact."""

    max_affected_row_pct: float | None = 10.0
    max_affected_rows: int | None = None


@dataclass(frozen=True)
class StatementDecision:
    index: int
    klass: ReversibilityClass
    decision: Decision
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome for a whole action: strictest statement decision, with human-readable reasons."""

    decision: Decision
    klass: ReversibilityClass
    reasons: tuple[str, ...]
    statements: tuple[StatementDecision, ...]


@dataclass(frozen=True)
class Policy:
    default_namespace: str = DEFAULT_NAMESPACE
    class_decisions: dict[ReversibilityClass, Decision] = field(
        default_factory=lambda: dict(DEFAULT_DECISIONS)
    )
    protected_tables: frozenset[str] = frozenset()
    retention: RetentionRule = field(default_factory=RetentionRule)
    table_retention: dict[str, RetentionRule] = field(default_factory=dict)
    thresholds: Thresholds = field(default_factory=Thresholds)
    task_scope: TaskScope | None = None
    invariants: InvariantsConfig = field(default_factory=InvariantsConfig)
    # namespace -> "dev" | "prod". Every write to a prod namespace needs approval, whatever its
    # size or class; a namespace that is not listed behaves as dev.
    environments: dict[str, str] = field(default_factory=dict)
    # An UPDATE or MERGE on a table without a key is shown to a reviewer as deleted plus added
    # rows, so it is not published without one ("unkeyed diff").
    require_table_key: bool = False
    version: str | None = None  # a label for humans; it takes no part in `digest`

    @property
    def digest(self) -> str:
        """sha256 of the canonical form of everything that influences a decision.

        Two policies that decide identically have the same digest, whatever order their YAML
        lists things in, and any change to a rule, threshold, scope or invariant changes it. A
        pending action records it so that a stricter (or looser) policy invalidates the review.
        """
        return digest(self.canonical())

    def canonical(self) -> dict[str, Any]:
        def scope(s: TaskScope | None) -> dict[str, list[str]] | None:
            if s is None:
                return None
            return {
                "allowed_tables": sorted(s.allowed_tables),
                "allowed_namespaces": sorted(s.allowed_namespaces),
            }

        canonical = {
            "default_namespace": self.default_namespace,
            "class_decisions": {k.value: v.value for k, v in sorted(self.class_decisions.items())},
            "protected_tables": sorted(self.protected_tables),
            "retention": asdict(self.retention),
            "table_retention": {t: asdict(r) for t, r in sorted(self.table_retention.items())},
            "thresholds": asdict(self.thresholds),
            "task_scope": scope(self.task_scope),
            "invariants": {
                "default": asdict(self.invariants.default),
                "tables": {t: asdict(i) for t, i in sorted(self.invariants.tables.items())},
            },
        }
        # only when set, so a policy without them keeps the digest it always had
        if self.environments:
            canonical["environments"] = dict(sorted(self.environments.items()))
        if self.require_table_key:
            canonical["require_table_key"] = True
        return canonical

    def __post_init__(self) -> None:
        decisions = self.class_decisions
        missing = [c for c in ReversibilityClass if c not in decisions]
        if missing:
            raise PolicyError(f"class_decisions missing: {[c.value for c in missing]}")
        if decisions[C.UNKNOWN] != Decision.DENY or decisions[C.OUT_OF_SCOPE] != Decision.DENY:
            raise PolicyError("unknown and out_of_scope must be deny (the guard fails closed)")
        if decisions[C.IRREVERSIBLE] not in (Decision.ASK, Decision.DENY):
            raise PolicyError("irreversible must be ask or deny; it can never be auto-allowed")
        bad = {ns: env for ns, env in self.environments.items() if env not in ENVIRONMENTS}
        if bad:
            raise PolicyError(f"environments must map to one of {sorted(ENVIRONMENTS)}: {bad}")

    # -- loading -------------------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as err:
            raise PolicyError(f"cannot read policy {path}: {err}") from err
        return cls.from_dict(data if data is not None else {})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Policy:
        if not isinstance(data, Mapping):
            raise PolicyError("policy must be a mapping")
        unknown = set(data) - _TOP_LEVEL_KEYS
        if unknown:
            raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
        namespace = str(data.get("default_namespace", DEFAULT_NAMESPACE)).strip().lower()

        decisions = dict(DEFAULT_DECISIONS)
        for key, value in _mapping(data, "class_decisions").items():
            try:
                decisions[ReversibilityClass(key)] = Decision(value)
            except ValueError as err:
                raise PolicyError(f"class_decisions: bad entry {key!r}: {value!r}") from err

        retention_cfg = _mapping(data, "retention")
        unknown_retention = set(retention_cfg) - {"default", "tables"}
        if unknown_retention:
            raise PolicyError(f"unknown retention keys: {sorted(unknown_retention)}")
        default_rule = _retention_rule(retention_cfg.get("default") or {}, RetentionRule())
        table_rules = {
            qualify_table_name(str(table), namespace): _retention_rule(rule or {}, default_rule)
            for table, rule in _mapping(retention_cfg, "tables").items()
        }

        scope_cfg = _mapping(data, "task_scope")
        scope = None
        if scope_cfg:
            unknown_scope = set(scope_cfg) - {"allowed_tables", "allowed_namespaces"}
            if unknown_scope:
                raise PolicyError(f"unknown task_scope keys: {sorted(unknown_scope)}")
            scope = TaskScope.of(
                _str_list(scope_cfg, "allowed_tables"),
                _str_list(scope_cfg, "allowed_namespaces"),
                namespace,
            )

        return cls(
            default_namespace=namespace,
            class_decisions=decisions,
            protected_tables=frozenset(
                qualify_table_name(t, namespace) for t in _str_list(data, "protected_tables")
            ),
            retention=default_rule,
            table_retention=table_rules,
            thresholds=_thresholds(_mapping(data, "thresholds")),
            task_scope=scope,
            invariants=_invariants(data.get("invariants"), namespace),
            environments=_environments(data),
            require_table_key=_flag(data, "require_table_key"),
            version=None if data.get("version") is None else str(data["version"]),
        )

    # -- decisions -----------------------------------------------------------------------------

    def retention_for(self, table: str) -> RetentionRule:
        return self.table_retention.get(table, self.retention)

    def decide(
        self,
        classification: Classification,
        task_scope: TaskScope | None = None,
        now: datetime | None = None,
    ) -> PolicyDecision:
        """Decide for `classification`; with no scope (argument or policy) no table is allowed."""
        scope = task_scope or self.task_scope or TaskScope()
        moment = _aware(now) if now is not None else datetime.now(UTC)
        multi = len(classification.statements) > 1
        decided = [
            self._decide_statement(s, scope, classification.est_impact, moment)
            for s in classification.statements
        ]
        if not decided:
            decided = [
                StatementDecision(0, C.UNKNOWN, Decision.DENY, ("action produced no statements",))
            ]
        reasons = tuple(
            f"[statement {d.index + 1}] {r}" if multi else r for d in decided for r in d.reasons
        )
        return PolicyDecision(
            decision=Decision.strictest([d.decision for d in decided]),
            klass=ReversibilityClass.most_severe([d.klass for d in decided]),
            reasons=reasons,
            statements=tuple(decided),
        )

    def _decide_statement(
        self, stmt: StatementClass, scope: TaskScope, impact: Impact | None, now: datetime
    ) -> StatementDecision:
        klass = stmt.klass
        reasons = list(stmt.reasons)

        if klass is not C.UNKNOWN:
            outside = [t for t in stmt.tables if not scope.allows(t)]
            protected = [t for t in stmt.write_tables if t in self.protected_tables]
            if outside:
                klass = C.OUT_OF_SCOPE
                reasons.append(f"tables outside the task scope: {', '.join(outside)}")
            if protected:
                klass = C.OUT_OF_SCOPE
                reasons.append(f"protected tables cannot be modified: {', '.join(protected)}")

        if klass is C.REVERSIBLE_WRITE and stmt.verb in ROW_MODIFYING_VERBS and impact is not None:
            exceeded = self._impact_exceeded(impact)
            if exceeded:
                klass = C.RISKY_WRITE
                reasons.append(exceeded)

        forced_deny = False
        if stmt.maintenance is not None and klass is not C.OUT_OF_SCOPE:
            table = stmt.write_tables[0] if stmt.write_tables else ""
            violations = _retention_violations(stmt.maintenance, self.retention_for(table), now)
            forced_deny = bool(violations)
            reasons.extend(f"retention violation: {v}" for v in violations)

        decision = Decision.DENY if forced_deny else self.class_decisions[klass]
        if decision in _AUTO and klass is not C.READ:
            for reason in self._approval_rules(stmt, impact):
                decision = Decision.ASK
                reasons.append(reason)
        return StatementDecision(stmt.index, klass, decision, tuple(reasons))

    def _approval_rules(self, stmt: StatementClass, impact: Impact | None) -> list[str]:
        """Why a write that the class would let through still needs a person."""
        why = []
        production = [t for t in stmt.write_tables if self.environment_of(t) == "prod"]
        if production:
            why.append(f"writes to production always need approval: {', '.join(production)}")
        if (
            self.require_table_key
            and stmt.verb in _KEY_PAIRED_VERBS
            and impact is not None
            and impact.keyed is False
            and impact.rows_affected != 0
        ):
            why.append(
                "unkeyed diff: the table has neither identifier fields nor a unique_key, so a "
                "reviewer would see each updated row as one deleted and one added row"
            )
        return why

    def environment_of(self, table: str) -> str:
        """`prod` when any namespace part of `table` is a production namespace, else `dev`."""
        if not self.environments:
            return "dev"
        namespace = table.rsplit(".", 1)[0] if "." in table else self.default_namespace
        parts = [namespace, *namespace.split(".")]
        return "prod" if any(self.environments.get(p) == "prod" for p in parts) else "dev"

    def _impact_exceeded(self, impact: Impact) -> str:
        """Return why the measured impact is over a limit, or '' when within limits."""
        max_pct, max_rows = self.thresholds.max_affected_row_pct, self.thresholds.max_affected_rows
        pct, rows = impact.pct, impact.rows_affected
        if max_pct is not None and pct is not None and pct > max_pct:
            return f"dry run affects {pct:.1f}% of rows (limit {max_pct:g}%)"
        if max_rows is not None and rows is not None and rows > max_rows:
            return f"dry run affects {rows} rows (limit {max_rows})"
        return ""


# -- helpers -----------------------------------------------------------------------------------


_AUTO = frozenset({Decision.ALLOW, Decision.ALLOW_VIA_BRANCH})


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _age_days(moment: datetime, now: datetime) -> float:
    return (now - _aware(moment)).total_seconds() / 86400


def _retention_violations(
    action: MaintenanceAction, rule: RetentionRule, now: datetime
) -> list[str]:
    """Reasons why destructive maintenance would breach the table's minimum retention."""
    problems: list[str] = []
    if isinstance(action, ExpireSnapshots):
        if action.snapshot_ids:
            problems.append("explicit snapshot_ids cannot be checked against minimum retention")
        if action.older_than is None:
            problems.append("older_than must be an explicit timestamp so retention can be verified")
        elif (age := _age_days(action.older_than, now)) < rule.min_snapshot_age_days:
            problems.append(
                f"older_than is {age:.1f} days old; minimum snapshot age is "
                f"{rule.min_snapshot_age_days:g} days"
            )
        retain = 1 if action.retain_last is None else action.retain_last
        if retain < rule.min_snapshots:
            problems.append(
                f"retain_last={retain} keeps fewer than the minimum {rule.min_snapshots} snapshots"
            )
    elif isinstance(action, RemoveOrphanFiles):
        if action.older_than is None:
            problems.append("older_than must be an explicit timestamp so retention can be verified")
        elif (age := _age_days(action.older_than, now)) < rule.min_orphan_age_days:
            problems.append(
                f"older_than is {age:.1f} days old; minimum orphan-file age is "
                f"{rule.min_orphan_age_days:g} days"
            )
    return problems


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PolicyError(f"{key} must be a mapping")
    return value


def _str_list(data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError(f"{key} must be a list of strings")
    return value


def _number(value: Any, name: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise PolicyError(f"{name} must be a number >= {minimum:g}")
    return float(value)


def _retention_rule(cfg: Mapping[str, Any], base: RetentionRule) -> RetentionRule:
    if not isinstance(cfg, Mapping):
        raise PolicyError("retention rule must be a mapping")
    unknown = set(cfg) - {"min_snapshot_age_days", "min_snapshots", "min_orphan_age_days"}
    if unknown:
        raise PolicyError(f"unknown retention rule keys: {sorted(unknown)}")
    return RetentionRule(
        min_snapshot_age_days=_number(
            cfg.get("min_snapshot_age_days", base.min_snapshot_age_days), "min_snapshot_age_days"
        ),
        min_snapshots=int(_number(cfg.get("min_snapshots", base.min_snapshots), "min_snapshots")),
        min_orphan_age_days=_number(
            cfg.get("min_orphan_age_days", base.min_orphan_age_days), "min_orphan_age_days"
        ),
    )


def _environments(data: Mapping[str, Any]) -> dict[str, str]:
    mapping = _mapping(data, "environments")
    namespace_names = [str(ns).strip().lower() for ns in mapping]
    if len(set(namespace_names)) != len(namespace_names):
        raise PolicyError("environments lists a namespace twice")
    return {str(ns).strip().lower(): str(env).strip().lower() for ns, env in mapping.items()}


def _flag(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise PolicyError(f"{key} must be true or false")
    return value


def _invariants(cfg: Any, namespace: str) -> InvariantsConfig:
    try:
        return InvariantsConfig.from_dict(cfg, namespace)
    except InvariantConfigError as err:
        raise PolicyError(str(err)) from err


def _thresholds(cfg: Mapping[str, Any]) -> Thresholds:
    unknown = set(cfg) - {"max_affected_row_pct", "max_affected_rows"}
    if unknown:
        raise PolicyError(f"unknown thresholds keys: {sorted(unknown)}")
    defaults = Thresholds()
    pct = cfg.get("max_affected_row_pct", defaults.max_affected_row_pct)
    rows = cfg.get("max_affected_rows", defaults.max_affected_rows)
    return Thresholds(
        max_affected_row_pct=None if pct is None else _number(pct, "max_affected_row_pct"),
        max_affected_rows=None if rows is None else int(_number(rows, "max_affected_rows")),
    )
