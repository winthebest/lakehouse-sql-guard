"""`sqlguard` command line.

    sqlguard leakage check queries/ --spec spec.yaml [--warehouse DIR | --catalog NAME]
        [--replay] [--json]
    sqlguard safety check "DELETE FROM orders" --policy policy.yaml --scope db.orders [--json]

Exit codes: 0 = clean / allowed, 1 = leaky or not allowed, 2 = usage or input error.

Each command group lives in its own module and plugs in with `register`, so adding a group
never touches this file or another group's module:

    def add_parser(subparsers):          # build and return the group's parser
        parser = subparsers.add_parser("approvals", help="...")
        ...
        return parser

    def handle(args) -> int:             # run the chosen command, return the exit code
        ...

    register("approvals", add_parser, handle)

The module is imported by `_load_groups`, which is the one line a new module needs.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import Any

# `argparse._SubParsersAction` is private; the registry only needs "something add_parser can be
# called on", so it is typed loosely.
AddParser = Callable[[Any], argparse.ArgumentParser]
Handler = Callable[[argparse.Namespace], int]

_REGISTRY: dict[str, tuple[AddParser, Handler]] = {}


def register(name: str, add_parser: AddParser, handler: Handler) -> None:
    """Add the command group `name`.

    `add_parser(subparsers)` creates the group's parser (and its own sub-commands) and returns
    it; `handler(args)` runs whichever command was chosen and returns the process exit code.
    """
    if name in _REGISTRY:
        raise ValueError(f"command group {name!r} is already registered")
    _REGISTRY[name] = (add_parser, handler)


def _load_groups() -> None:
    # importing a module registers its group; `--help` lists groups in registration order
    from sqlguard.cli import approvals, leakage, safety  # noqa: F401


def build_parser() -> argparse.ArgumentParser:
    _load_groups()
    parser = argparse.ArgumentParser(prog="sqlguard", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="module", required=True)
    for add_parser, handler in _REGISTRY.values():
        add_parser(sub).set_defaults(handler=handler)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        code = args.handler(args)
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        code = 2
    raise SystemExit(code)
