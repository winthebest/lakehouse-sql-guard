"""Which model holds a leak the analysis found in the inlined query?

A finding names a table reference (`orders AS o`), and that reference usually sits in a thin
staging model, far from where the history is aggregated or joined to the spine. The model to fix
is the one that first does something to the rows on their way from the table to the training set:
walking up from the model that reads the table, along each path the rows take, it is the first
whose query is more than a projection of one relation (it joins, filters, groups, aggregates,
windows, ...); when every model on the way is a plain projection, the training model.

Findings that name no table are placed by what they say: a wall-clock use (L7) by the models whose
SQL calls the clock, a feature derived from the target (L6) like the label reference it reads.
Anything else is attributed to the training model. When one table reaches the training set along
several paths, every path's model is listed.
"""

from __future__ import annotations

from sqlglot import exp

from sqlguard.core.findings import Finding
from sqlguard.dbt.inline import OWNER, Inlined
from sqlguard.dbt.manifest import Node
from sqlguard.leakage.analysis import _CLOCK_FUNCTIONS, _CLOCK_NODES, QueryAnalysis, TableRef

_ROW_CLAUSES = ("joins", "where", "group", "having", "qualify", "distinct", "limit", "offset")


def shapes_rows(query: exp.Expression, unique_id: str) -> bool:
    """Is the model's own query more than a projection of a single relation?"""
    if not isinstance(query, exp.Select) or any(query.args.get(k) for k in _ROW_CLAUSES):
        return True
    own = [n for n in query.walk() if n.meta.get(OWNER) == unique_id]
    return any(isinstance(n, (exp.AggFunc, exp.Window)) for n in own) or (
        sum(isinstance(n, exp.Select) for n in own) > 1
    )


def owner(node: exp.Expression | None, default: str) -> str:
    """The model whose SQL `node` came from (nodes the inliner made belong to their parent)."""
    while node is not None:
        found = node.meta.get(OWNER)
        if found:
            return found
        node = node.parent
    return default


class Attributor:
    def __init__(self, inlined: Inlined, analysis: QueryAnalysis) -> None:
        self.inlined = inlined
        self.analysis = analysis
        self.root = inlined.root.unique_id
        resolver = analysis.resolver
        self.scopes = (
            {id(scope): scope for scope in resolver.by_expression.values()} if resolver else {}
        )

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
                (m for m in dict.fromkeys(chain) if shapes_rows(self.inlined.queries[m], m)),
                self.root,
            )
            if pick not in found:
                found.append(pick)
        return found

    def _clock_models(self) -> list[str]:
        found = {
            owner(n, self.root)
            for n in self.inlined.tree.walk()
            if isinstance(n, _CLOCK_NODES)
            or (isinstance(n, exp.Anonymous) and str(n.this).lower() in _CLOCK_FUNCTIONS)
        }
        return sorted(found)

    def models(self, finding: Finding) -> list[Node]:
        """The models a finding (or a fix, via its rule and location) is about."""
        refs = [r for r in self.analysis.refs if r.describe() == finding.location]
        if not refs and finding.rule_id == "L6":
            tables = {str(s).split(".")[0] for s in finding.evidence.get("sources", [])}
            refs = [r for r in self.analysis.refs if r.table in tables]
        ids: list[str] = []
        if refs:
            ids = [m for ref in refs for m in self._locus(ref)]
        elif finding.rule_id == "L7":
            ids = self._clock_models()
        found = [self.inlined.models[m] for m in dict.fromkeys(ids)]
        return found or [self.inlined.root]
