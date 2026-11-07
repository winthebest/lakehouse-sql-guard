"""Checks on a round 3 spec that declares `output_roles`, and the spec without the roles.

A role removes a column from what the checker treats as a feature, so a spec author could silence a
warning by declaring it. Every declared column must therefore cite the document that says what
it is: a comment with `path:line` on the column's own line or on the comment lines right above
it. Roles are written one column per line so each has a place for its citation.

`strip_output_roles` produces `specs-noroles/<repo>.yaml` from `specs/<repo>.yaml`: the same text
without the `output_roles` block, so a report can show how much the roles contribute.

Usage: python -m bench.realworld.round3.spec_gate SPEC.yaml ...   (prints the problems)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# something.ext:123, with a path that may contain directories
_CITATION = re.compile(r"(?<![\w/])[\w./-]*\w\.[A-Za-z0-9]+:\d+(?!\w)")
_URL = re.compile(r"https?://\S+")
_BLOCK = re.compile(r"^output_roles\s*:\s*(#.*)?$")
_ROLE = re.compile(r"^\s+(label|metadata|features)\s*:\s*(.*?)\s*(#.*)?$")
_ITEM = re.compile(r"^\s+-\s+([^\s#]+)\s*(#.*)?$")


def _cites(comment: str) -> bool:
    return _CITATION.search(_URL.sub("", comment)) is not None


def _roles_block(lines: list[str]) -> tuple[int, int] | None:
    """`[start, end)` line numbers of the top-level `output_roles` block, if any."""
    for start, line in enumerate(lines):
        if _BLOCK.match(line):
            end = start + 1
            while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
                end += 1
            return start, end
    return None


def check_role_citations(path: Path) -> list[str]:
    """What is wrong with the roles of a spec file (empty when all is well or there are none)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    block = _roles_block(lines)
    if block is None:
        return []
    problems = []
    above: list[str] = []  # comment lines directly above the current line
    for number in range(block[0] + 1, block[1]):
        line = lines[number]
        stripped = line.strip()
        if stripped.startswith("#"):
            above.append(stripped)
            continue
        if not stripped:
            above = []
            continue
        role = _ROLE.match(line)
        if role:
            inline = role.group(2).strip()
            if inline:  # `metadata: [a, b]` or `label: a`
                problems.append(
                    f"{path}:{number + 1}: write role {role.group(1)!r} one column per line "
                    f"(`- column  # path:line`), so each column has a citation"
                )
            above = []
            continue
        item = _ITEM.match(line)
        if item:
            comment = item.group(2) or ""
            if not _cites(comment) and not any(_cites(c) for c in above):
                problems.append(
                    f"{path}:{number + 1}: output role column {item.group(1)!r} cites no "
                    f"document (add `# path:line`)"
                )
            above = []
            continue
        problems.append(f"{path}:{number + 1}: cannot read this output_roles line: {stripped!r}")
        above = []
    return problems


def strip_output_roles(text: str) -> str:
    """`text` without its top-level `output_roles` block (comments inside it included)."""
    lines = text.splitlines(keepends=True)
    block = _roles_block([line.rstrip("\n") for line in lines])
    if block is None:
        return text
    return "".join(lines[: block[0]] + lines[block[1] :])


def main(argv: list[str] | None = None) -> int:
    paths = [Path(a) for a in (sys.argv[1:] if argv is None else argv)]
    problems = [p for path in paths for p in check_role_citations(path)]
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
