"""Column qualification and column-level lineage on top of sqlglot.

All sqlglot optimizer/lineage calls live here so a sqlglot upgrade only touches one file.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.lineage import lineage as sqlglot_lineage
from sqlglot.optimizer.qualify import qualify

from sqlguard.core.parsing import DEFAULT_DIALECT, parse_single

# {table_name: {column_name: sql_type}} — tables are referenced by bare name in guarded SQL.
Schema = dict[str, dict[str, str]]


class LineageError(ValueError):
    """Raised when SQL references tables or columns that the schema does not know."""


@dataclass(frozen=True, order=True)
class SourceColumn:
    """A physical column in a source table."""

    table: str
    column: str


def qualify_statement(
    sql_or_ast: str | exp.Expression, schema: Schema, dialect: str = DEFAULT_DIALECT
) -> exp.Expression:
    """Return a copy of the statement with every column bound to its source table.

    `SELECT *` is expanded using the schema. Unknown tables/columns raise LineageError.
    """
    ast = parse_single(sql_or_ast, dialect) if isinstance(sql_or_ast, str) else sql_or_ast.copy()
    try:
        return qualify(ast, schema=schema, dialect=dialect, validate_qualify_columns=True)
    except (OptimizeError, SqlglotError) as err:
        raise LineageError(f"Cannot qualify statement against schema: {err}") from err


def output_columns(qualified: exp.Expression) -> list[str]:
    """Names of the columns a (qualified) SELECT produces."""
    return list(qualified.named_selects)


def column_lineage(
    qualified: exp.Expression, column: str, schema: Schema, dialect: str = DEFAULT_DIALECT
) -> set[SourceColumn]:
    """Return the physical source columns that feed output `column`.

    Follows CTEs, subqueries, and aliases down to base tables.
    """
    try:
        root = sqlglot_lineage(column, qualified, schema=schema, dialect=dialect)
    except (SqlglotError, ValueError) as err:
        raise LineageError(f"Cannot compute lineage for column {column!r}: {err}") from err
    sources: set[SourceColumn] = set()
    for node in root.walk():
        if node.downstream or not isinstance(node.source, exp.Table):
            continue
        column_name = node.name.split(".")[-1].strip('"')
        sources.add(SourceColumn(table=node.source.name, column=column_name))
    return sources


def full_lineage(
    qualified: exp.Expression, schema: Schema, dialect: str = DEFAULT_DIALECT
) -> dict[str, set[SourceColumn]]:
    """Lineage for every output column of a SELECT."""
    return {
        col: column_lineage(qualified, col, schema, dialect) for col in output_columns(qualified)
    }
