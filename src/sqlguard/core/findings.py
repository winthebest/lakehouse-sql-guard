"""Result types shared by the leakage and safety modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """How serious a finding is. Ordered from least to most severe."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return list(Severity).index(self)


class Decision(StrEnum):
    """What the guard allows an action to do. Ordered from least to most restrictive."""

    ALLOW = "allow"
    ALLOW_VIA_BRANCH = "allow_via_branch"
    ASK = "ask"
    DENY = "deny"

    @property
    def rank(self) -> int:
        return list(Decision).index(self)

    @staticmethod
    def strictest(decisions: list[Decision]) -> Decision:
        """Return the most restrictive decision; an empty list is treated as DENY (fail closed)."""
        return max(decisions, key=lambda d: d.rank) if decisions else Decision.DENY


@dataclass(frozen=True)
class Finding:
    """A single issue raised by a rule, with enough evidence for a human to act on it."""

    rule_id: str
    severity: Severity
    message: str
    location: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data
