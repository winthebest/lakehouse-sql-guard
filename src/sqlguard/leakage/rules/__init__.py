"""Static leakage rules. Each module exposes `check(analysis) -> list[Finding]`."""

from sqlguard.leakage.rules import (
    boundary,
    ingestion_hint,
    join_time,
    nondeterministic_time,
    target_lineage,
    unbounded_agg,
)

ALL_RULES = (
    join_time,
    unbounded_agg,
    boundary,
    target_lineage,
    ingestion_hint,
    nondeterministic_time,
)

__all__ = ["ALL_RULES"]
