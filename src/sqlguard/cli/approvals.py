"""Command group `sqlguard approvals` (reserved).

Nothing is registered yet, so the command does not appear in `--help`. A change that adds it
calls `register("approvals", add_parser, handle)` from `sqlguard.cli` at import time; this module
is already imported by `sqlguard.cli.build_parser`.
"""
