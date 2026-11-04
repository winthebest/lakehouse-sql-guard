"""Parse SQL into statements without executing anything.

Every input is split into individual statements so callers can never be fooled by
`SELECT 1; COMMIT; DROP TABLE t` style payloads that hide a write behind a read.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

DEFAULT_DIALECT = "duckdb"

# sqlglot logs a warning whenever it falls back to an opaque `Command` node (e.g. Spark `CALL`).
# The guard handles `Command` explicitly, so the warning is noise for library users.
logging.getLogger("sqlglot").setLevel(logging.ERROR)


class SqlParseError(ValueError):
    """Raised when SQL cannot be parsed. Callers in the safety path must treat this as DENY."""


@dataclass(frozen=True)
class ParsedStatement:
    """One SQL statement: its AST node class name, the AST, and normalized SQL text."""

    kind: str
    ast: exp.Expression
    sql: str


def parse(sql: str, dialect: str = DEFAULT_DIALECT) -> list[ParsedStatement]:
    """Split `sql` into statements and parse each one.

    Raises SqlParseError for syntax errors or input with no statements.
    """
    try:
        trees = sqlglot.parse(sql, dialect=dialect)
    except ParseError as err:
        raise SqlParseError(f"Could not parse SQL ({dialect}): {err}") from err
    statements = [
        ParsedStatement(kind=type(tree).__name__, ast=tree, sql=tree.sql(dialect=dialect))
        for tree in trees
        # sqlglot emits a bare Semicolon node for comments between statements; it carries no work.
        if tree is not None and not isinstance(tree, exp.Semicolon)
    ]
    if not statements:
        raise SqlParseError("No SQL statement found")
    return statements


def parse_single(sql: str, dialect: str = DEFAULT_DIALECT) -> exp.Expression:
    """Parse exactly one statement; reject multi-statement input."""
    statements = parse(sql, dialect)
    if len(statements) != 1:
        raise SqlParseError(f"Expected one statement, got {len(statements)}")
    return statements[0].ast


# Options that may sit between EXPLAIN and the explained statement, across DuckDB, Spark,
# Trino and PostgreSQL. A parenthesized option list may only hold plain words and commas, so
# re-emitting it can never smuggle in other SQL.
_EXPLAIN_OPTIONS = re.compile(
    r"\s*(?:\((?P<paren>[A-Za-z0-9_ ,]*)\)\s*)?"
    r"(?P<words>(?:(?:ANALYZE|ANALYSE|VERBOSE|EXTENDED|FORMATTED|CODEGEN|COST|LOGICAL"
    r"|DISTRIBUTED|IO)\b\s*)*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Explain:
    """An EXPLAIN statement split into its options and the statement it explains.

    `analyze` is True when the engine will also run the statement (EXPLAIN ANALYZE), so the
    explained statement must be judged as if it ran.
    """

    options: str
    analyze: bool
    statement: ParsedStatement

    def render(self, statement_sql: str) -> str:
        return " ".join(part for part in ("EXPLAIN", self.options, statement_sql) if part)


def explained(ast: exp.Expression, dialect: str = DEFAULT_DIALECT) -> Explain | None:
    """The statement under an EXPLAIN, or None when `ast` is not an EXPLAIN.

    sqlglot keeps EXPLAIN as an opaque command, so the explained text is parsed here. Raises
    SqlParseError unless it is exactly one statement.
    """
    if not (isinstance(ast, exp.Command) and str(ast.this).upper() == "EXPLAIN"):
        return None
    body = ast.expression
    text = body.name if isinstance(body, exp.Literal) else ""
    match = _EXPLAIN_OPTIONS.match(text)
    prefix = text[: match.end()]
    inner = parse(text[match.end() :], dialect)
    if len(inner) != 1:
        raise SqlParseError(f"EXPLAIN must wrap exactly one statement, got {len(inner)}")
    return Explain(
        options=" ".join(prefix.split()),
        analyze=re.search(r"\bANALY[SZ]E\b", prefix, re.IGNORECASE) is not None,
        statement=inner[0],
    )
