"""Which model holds a leak the analysis found in the inlined query?

A finding names a table reference (`orders AS o`), and that reference usually sits in a thin
staging model, far from where the history is aggregated or joined to the spine. The model to fix
is the one that introduces the unbounded read: walking up from the model that reads the table,
along each path the rows take, it is the first that

- reads the label spine (the bound belongs where the spine and the table meet), or
- aggregates, windows, de-duplicates, limits or combines sets (it collapses the history),

so a staging model that only filters or joins a static table is passed over. When no model on the
way does either, the training model is named, and the answer is marked uncertain.

Findings that name no table are placed by what they say: a wall-clock use (L7) by the models whose
SQL calls the clock, a feature derived from the target (L6) like the label reference it reads.
Anything else is attributed to the training model. When one table reaches the training set along
several paths, every path's model is listed, and the answer is marked as candidates.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from sqlguard.core.findings import Finding
from sqlguard.dbt.inline import OWNER, Inlined
from sqlguard.dbt.manifest import Node
from sqlguard.leakage.analysis import _CLOCK_FUNCTIONS, _CLOCK_NODES, QueryAnalysis, TableRef

_COLLAPSING_CLAUSES = ("group", "having", "qualify", "distinct", "limit", "offset")


def collapses_history(query: exp.Expression, unique_id: str) -> bool:
    """Does the model's own query aggregate, window, de-duplicate, limit or combine sets?"""
    if not isinstance(query, exp.Select):
        return True
    if any(query.args.get(k) for k in _COLLAPSING_CLAUSES):
        return True
    own = (n for n in query.walk() if n.meta.get(OWNER) == unique_id)
    return any(isinstance(n, (exp.AggFunc, exp.Window)) for n in own)


def owner(node: exp.Expression | None, default: str) -> str:
    """The model whose SQL `node` came from (nodes the inliner made belong to their parent)."""
    while node is not None:
        found = node.meta.get(OWNER)
        if found:
            return found
        node = node.parent
    return default


@dataclass(frozen=True)
class Attribution:
    models: list[Node]
    certain: bool  # False: candidates, not a single model that was identified


class Attributor:
    def __init__(self, inlined: Inlined, analysis: QueryAnalysis) -> None:
        self.inlined = inlined
        self.certain = True
        self.analysis = analysis
        self.root = inlined.root.unique_id
        # models whose own SQL reads a label table: where the spine meets the history
        self.spine_readers = {
            owner(ref.node, self.root) for ref in analysis.refs if ref.kind == "label"
        }
        resolver = analysis.resolver
        self.scopes = (
            {id(scope): scope for scope in resolver.by_expression.values()} if resolver else {}
        )

    def _meets_spine(self, unique_id: str) -> bool:
        return unique_id in self.spine_readers

    def _locus(self, ref: TableRef) -> list[str]:
        """The models to fix for one table reference, one per path the rows take."""
        paths = ref.temporal_paths or ref.paths
        found: list[str] = []
        for path in paths:
            chain = [owner(ref.node, self.root)]
            for scope_id, _alias in reversed(path):
                scope = self.scopes.get(scope_id)
                if scope is not None:
                    chain.append(owner(scope.expression, self.root))
            pick = next(
                (
                    m
                    for m in dict.fromkeys(chain)
                    if self._meets_spine(m) or collapses_history(self.inlined.queries[m], m)
                ),
                None,
            )
            if pick is None:
                self.certain = False
            if (pick or self.root) not in found:
                found.append(pick or self.root)
        return found

    def _clock_models(self) -> list[str]:
        found = {
            owner(n, self.root)
            for n in self.inlined.tree.walk()
            if isinstance(n, _CLOCK_NODES)
            or (isinstance(n, exp.Anonymous) and str(n.this).lower() in _CLOCK_FUNCTIONS)
        }
        return sorted(found)

    def models(self, finding: Finding) -> Attribution:
        """The models a finding (or a fix, via its rule and location) is about."""
        self.certain = True
        refs = [r for r in self.analysis.refs if r.describe() == finding.location]
        if not refs and finding.rule_id == "L6":
            tables = {str(s).split(".")[0] for s in finding.evidence.get("sources", [])}
            refs = [r for r in self.analysis.refs if r.table in tables]
        ids: list[str] = []
        if refs:
            ids = [m for ref in refs for m in self._locus(ref)]
        elif finding.rule_id == "L7":
            ids = self._clock_models()
        else:
            self.certain = finding.rule_id in ("REPLAY", "PARSE", "SPEC", "LINEAGE")
        ids = list(dict.fromkeys(ids))
        found = [self.inlined.models[m] for m in ids]
        return Attribution(found or [self.inlined.root], self.certain and len(found) <= 1)
