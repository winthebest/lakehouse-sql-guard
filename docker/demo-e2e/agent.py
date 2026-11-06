"""The scripted agent of the end-to-end demo: a real MCP client over stdio, no model, no key.

    agent.py '[{"tool": "run_sql", "args": {"sql": "DELETE ..."}, "expect": "pending_approval"}]'

It starts `sqlguard-mcp` exactly as an IDE would (the server flags come from the SQLGUARD_MCP_ARGS
environment variable, a JSON list) and makes the calls in order within one session, which is one
"turn" of an agent. The tools are the ones a model sees, and so is every answer. `expect` is the
status the call must return; anything else ends the turn with exit code 1.

It prints what the agent sent and what came back, shortened to the fields that matter.
"""

from __future__ import annotations

import json
import os
import sys

import anyio
from mcp import Client, StdioServerParameters

SHOWN_REASONS = 3


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_result(tool: str, payload: dict) -> list[str]:
    """The lines to show for one tool answer: status first, then what a reader needs."""
    if tool == "list_tables":
        return [f"tables: {', '.join(payload.get('tables', []))}"]
    if tool == "describe_table":
        return [
            f"{payload.get('table', '')}: {payload.get('row_count', '?')} rows, "
            f"main is on snapshot {payload.get('current_snapshot_id')}"
        ]
    state = f"   state: {payload['state']}" if "state" in payload else ""
    lines = [f"status: {payload.get('status', '?')}{state}"]
    for key in ("action_id", "summary"):
        if payload.get(key):
            lines.append(f"{key}: {payload[key]}")
    for reason in (payload.get("reasons") or [])[:SHOWN_REASONS]:
        lines.append(f"reason: {shorten(str(reason), 110)}")
    if payload.get("message"):
        lines.append(f"message: {str(payload['message']).split('. ')[0].rstrip('.')}.")
    return lines


async def run(calls: list[dict]) -> int:
    args = json.loads(os.environ["SQLGUARD_MCP_ARGS"])
    # An IDE hands the server its catalog settings through the `env` of its MCP entry; so does this.
    env = {k: v for k, v in os.environ.items() if k.startswith("PYICEBERG_")}
    params = StdioServerParameters(command="sqlguard-mcp", args=args, env=env)
    async with Client(params) as client:
        for call in calls:
            tool, arguments = call["tool"], call.get("args", {})
            shown = json.dumps(arguments) if arguments else ""
            print(f"agent> {tool} {shown}".rstrip(), flush=True)
            answer = await client.call_tool(tool, arguments)
            payload = (answer.structured_content or {}).get("payload") or {}
            if answer.is_error and not payload:
                print(f"server error: {answer.content}", file=sys.stderr)
                return 1
            for line in render_result(tool, payload):
                print(line, flush=True)
            if call.get("expect") and payload.get("status") != call["expect"]:
                print(
                    f"expected status {call['expect']}, got {payload.get('status')}",
                    file=sys.stderr,
                )
                return 1
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    try:
        return anyio.run(run, json.loads(sys.argv[1]))
    except BaseExceptionGroup as group:  # a server that did not start: say why, once
        print(f"agent: cannot talk to sqlguard-mcp: {group.exceptions[0]!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
