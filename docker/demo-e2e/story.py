"""The storyline of the end-to-end demo. Started by docker/demo-e2e.sh; needs only Python 3.10+.

The stack (Lakekeeper, an S3-compatible store) and the `runner` container are up already. This
program runs the story by executing commands in the runner (`docker exec`) and by using the
approval web UI like a person would, over the loopback port that the runner publishes:

  Leakage   load nightly commits with late rows -> dbt build a training set -> `sqlguard leakage
            dbt` finds the leak in an intermediate model and suggests the fix -> apply -> clean.
  Safety    a scripted MCP agent asks to delete production data -> the action is held, the UI shows
            the row diff, the reviewer rejects -> a narrower request is approved and published by
            snapshot id -> someone else writes in between and the next approval ends stale -> a
            rollback is requested, approved and done.

Everything printed below a headline is what the tools said. With `--agent claude` the agent turns
are Claude Code in headless mode, on the host, speaking MCP through `docker exec`; it needs
`claude` logged in or an API key and is never run by the tests.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import reviewer  # noqa: E402

UI_PORT = 18765
STEPS = 11
MCP_SERVER = "lakehouse-guard"
CATALOG_NAME = "lakehouse"
IN_RUNNER_STATE = "/state"
POLICY = "/work/docker/demo-e2e/policy.yaml"
DEMO = "/work/docker/demo-e2e"
PROJECT = f"{IN_RUNNER_STATE}/dbt"
CHECK = [
    "sqlguard", "leakage", "dbt", "--manifest", "build_ci/manifest.json", "--replay",
    "--catalog", CATALOG_NAME, "--catalog-namespace", "raw",
]  # fmt: skip
NARROW_DELETE = (
    "DELETE FROM prod.orders WHERE status = 'cancelled' AND customer_id = 1 AND id <= 30"
)
WIDE_DELETE = "DELETE FROM prod.orders WHERE status = 'cancelled'"
UPDATE = "UPDATE prod.orders SET status = 'refunded' WHERE id = 7"
TOOLS = ("list_tables", "describe_table", "run_sql", "rollback", "get_action_status")
DENIED_TOOLS = ("expire_snapshots", "remove_orphan_files", "check_training_sql")
BUILTIN_TOOLS = (
    "Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "NotebookEdit",
    "Task", "Agent",
)  # fmt: skip


class StepFailed(RuntimeError):
    """The storyline did not go the way it should; the message says where."""


# -- pure helpers (tested in tests/test_demo_e2e.py) -------------------------------------------


def mcp_server_args(review_url: str) -> list[str]:
    """The flags `sqlguard-mcp` runs with in the runner, as an IDE's MCP entry would list them."""
    return [
        "--catalog", CATALOG_NAME, "--namespace", "prod", "--scope-namespace", "prod",
        "--policy", POLICY,
        "--audit-log", f"{IN_RUNNER_STATE}/audit.jsonl",
        "--store", f"{IN_RUNNER_STATE}/actions.db",
        "--review-url", review_url, "--requester", "demo-agent",
    ]  # fmt: skip


def claude_mcp_config(runner: str, server_args: Sequence[str]) -> dict:
    """The MCP entry for Claude Code on the host: the server runs in the runner, over stdio."""
    return {
        "mcpServers": {
            MCP_SERVER: {
                "type": "stdio",
                "command": "docker",
                "args": ["exec", "-i", "-w", "/work", runner, "sqlguard-mcp", *server_args],
            }
        }
    }


def claude_command(prompt: str, config: Path | str, allowed: Iterable[str] = TOOLS) -> list[str]:
    """Claude Code headless, held to the MCP tools of the guard.

    `--tools ""` removes every built-in tool; `--allowedTools` lists the MCP tools the task
    needs; `--disallowedTools` repeats the built-ins and adds the maintenance tools as deny rules;
    `dontAsk` turns anything that is not allowed into a refusal instead of a prompt;
    `--strict-mcp-config` ignores every other MCP server on the machine; a budget caps one turn.
    """
    allow = ",".join(f"mcp__{MCP_SERVER}__{name}" for name in allowed)
    deny = ",".join([*BUILTIN_TOOLS, *(f"mcp__{MCP_SERVER}__{n}" for n in DENIED_TOOLS)])
    return [
        "claude", "-p", prompt,
        "--mcp-config", str(config), "--strict-mcp-config",
        "--tools", "",
        "--allowedTools", allow,
        "--disallowedTools", deny,
        "--permission-mode", "dontAsk",
        "--max-budget-usd", "1",
        "--no-session-persistence",
    ]  # fmt: skip


def replay_summary(entry: dict) -> str:
    """One line on the replay of a `sqlguard leakage dbt --json` entry."""
    replay = entry.get("replay") or {}
    status = replay.get("status", "not run")
    if status == "pass":
        return (
            f"replay: pass - {replay['rows_compared']} rows over {replay['points_checked']} "
            "prediction dates equal what was knowable then"
        )
    if status == "mismatch":
        columns = ", ".join(sorted(replay.get("mismatched_columns", {})))
        return (
            f"replay: MISMATCH - {replay['rows_mismatched']} of {replay['rows_compared']} rows "
            f"differ at {replay['points_checked']} prediction dates ({columns})"
        )
    return f"replay: {status}"


_STATE = re.compile(
    r"catalog> (?P<table>\S+): (?P<rows>\d+) rows \((?P<cancelled>\d+) cancelled\); "
    r"main is on snapshot (?P<snapshot>\d+)"
)


def parse_catalog_state(text: str) -> dict[str, int | str]:
    """Rows, cancelled rows and the main snapshot from `lake.py state` output."""
    match = _STATE.search(text)
    if match is None:
        raise ValueError("no table state in the output of lake.py")
    return {
        "table": match["table"],
        "rows": int(match["rows"]),
        "cancelled": int(match["cancelled"]),
        "snapshot": match["snapshot"],
    }


def page_summary(page: reviewer.ActionPage, rows: int = 4) -> list[str]:
    """What the reviewer sees on an action page, in a few lines."""
    lines = [
        f"action {page.action_id[:8]}  [{page.state}]  {page.table}  requested by {page.requester}",
        f"snapshots: base {page.base_snapshot} -> head {page.head_snapshot}",
    ]
    if page.counts:
        lines.append(f"row diff: {page.counts}")
    lines += [f"  {row}" for row in page.rows[:rows]]
    if len(page.rows) > rows:
        lines.append(f"  ... {len(page.rows) - rows} more rows on this page")
    lines += [f"why: {reason}" for reason in page.reasons[:3]]
    lines += [f"BLOCKED: {problem}" for problem in page.problems]
    return lines


def audit_lines(records: Iterable[dict]) -> list[str]:
    """The finished entries of the audit log: who decided what, and whether it was published."""
    lines = []
    for record in records:
        if record.get("phase") != "completed" or record.get("outcome") == "reviewer_note":
            continue
        action = record.get("action") or {}
        text = action.get("sql") or action.get("kind") or action.get("type") or ""
        who = record.get("approver") or "-"
        lines.append(
            f"{str(record.get('outcome')):<18} by={who:<14} "
            f"published={str(record.get('published')):<5} {str(text)[:56]}"
        )
    return lines


def indent(line: str, pad: str = "    ") -> str:
    return pad + line.rstrip("\n")


def elapsed(seconds: float) -> str:
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s" if minutes else f"{rest}s"


# -- the story -------------------------------------------------------------------------------


class Story:
    def __init__(self, args: argparse.Namespace) -> None:
        self.runner: str = args.runner
        self.state = Path(args.state)
        self.agent: str = args.agent
        self.shots = Path(args.screenshots) if args.screenshots else None
        self.review_url = f"http://127.0.0.1:{UI_PORT}"
        self.ui: reviewer.Reviewer | None = None
        self.started = time.monotonic()
        self.step_no = 0
        self.tty = sys.stdout.isatty()

    # output --------------------------------------------------------------------------------

    def style(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.tty else text

    def headline(self, text: str) -> None:
        self.step_no += 1
        print(f"\n{self.style(f'== {self.step_no}/{STEPS}  {text}', '1;36')}", flush=True)

    def say(self, text: str) -> None:
        print(self.style(f"   # {text}", "90"), flush=True)

    def show(self, line: str) -> None:
        print(indent(line), flush=True)

    # running commands -----------------------------------------------------------------------

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: str = "/work",
        env: dict[str, str] | None = None,
        expect: Iterable[int] = (0,),
        echo: str | None = None,
        keep: Callable[[str], bool] | None = None,
        quiet: bool = False,
    ) -> tuple[int, str]:
        """Run `cmd` in the runner, streaming what it prints, and check its exit code."""
        docker = ["docker", "exec", "-w", cwd]
        for key, value in (env or {}).items():
            docker += ["-e", f"{key}={value}"]
        if not quiet:
            shown = echo if echo is not None else shlex.join(cmd)
            print(self.style(f"   $ {shown}", "33"), flush=True)
        done = subprocess.Popen(
            [*docker, self.runner, *cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        captured: list[str] = []
        assert done.stdout is not None
        for line in done.stdout:
            captured.append(line)
            if not quiet and (keep is None or keep(line)):
                self.show(line)
        code = done.wait()
        if code not in tuple(expect):
            raise StepFailed(f"`{shlex.join(cmd)}` exited {code}, expected {tuple(expect)}")
        return code, "".join(captured)

    def table_state(self, *, quiet: bool = False) -> dict[str, int | str]:
        """Rows and the main snapshot of prod.orders, read straight from the catalog."""
        _, out = self.run(["python", f"{DEMO}/lake.py", "state"], quiet=quiet)
        return parse_catalog_state(out)

    # part 1: leakage ------------------------------------------------------------------------

    def leakage(self) -> None:
        self.headline("Load the lakehouse as nightly commits, with late rows")
        self.say("Orders land through a nightly job: one Iceberg commit per night, and 1 in 5 rows")
        self.say("arrive 1-3 days late. Real commits, written to Lakekeeper over REST.")
        self.say("They are written seconds apart, so each records the night it stands for in the")
        self.say("snapshot property sqlguard.logical-ts; replay pins to that, not to commit time.")
        self.run(
            ["python", "/work/examples/dbt-lakehouse/load-lakehouse.py", "--catalog", "lakehouse"]
        )
        self.run(["python", f"{DEMO}/lake.py", "history", "raw.orders"])

        self.headline("dbt build a small training-set project")
        self.say("examples/dbt-lakehouse: raw sources -> int_customer_orders_7d -> training_churn.")
        self.run(
            ["sh", "-c", f"rm -rf {PROJECT} && cp -r /work/examples/dbt-lakehouse {PROJECT}"],
            quiet=True,
        )
        build = [
            "dbt",
            "build",
            "--profiles-dir",
            ".",
            "--target-path",
            "build_ci",
            "--no-use-colors",
        ]
        self.run(build, cwd=PROJECT)
        self.say("Every dbt test passes. Nothing in dbt knows the feature uses late-arriving rows.")

        self.headline("sqlguard leakage dbt: the leak, where it is, and a fix")
        self.say("The finding is attributed to the intermediate model, not to training_churn,")
        self.say("and replay over the snapshot history confirms the feature values differ.")
        self.run([*CHECK, "--suggest-fix"], cwd=PROJECT, expect=(1,))
        self.replay_line()

        self.headline("Apply the suggested fix, rebuild, check again")
        self.say("The suggested change adds one line to the intermediate model's join:")
        model = "models/intermediate/int_customer_orders_7d.sql"
        diff = ["diff", "-u", "--label", model, "--label", "suggested", model]
        self.run([*diff, "fix/int_customer_orders_7d.sql"], cwd=PROJECT, expect=(1,))
        self.run(["cp", "fix/int_customer_orders_7d.sql", model], cwd=PROJECT)
        self.run(build, cwd=PROJECT, keep=lambda line: "Done." in line or "ERROR" in line)
        self.run(CHECK, cwd=PROJECT, expect=(0,))
        self.replay_line()

    def replay_line(self) -> None:
        _, out = self.run([*CHECK, "--json"], cwd=PROJECT, expect=(0, 1), quiet=True)
        (entry,) = json.loads(out)
        self.show(replay_summary(entry))

    # part 2: the agent and the reviewer ----------------------------------------------------

    def start_ui(self) -> None:
        self.headline("Start the stack's approval UI and seed prod.orders")
        self.say("policy.yaml: every write to the namespace `prod` waits for a person.")
        self.run(["python", f"{DEMO}/lake.py", "seed"])
        serve = [
            "sqlguard", "serve", "--catalog", CATALOG_NAME, "--catalog-namespace", "prod",
            "--store", f"{IN_RUNNER_STATE}/actions.db", "--policy", POLICY,
            "--audit-log", f"{IN_RUNNER_STATE}/audit.jsonl", "--approver", "demo-reviewer",
            "--host", "0.0.0.0", "--i-know-this-is-unauthenticated", "--port", str(UI_PORT),
        ]  # fmt: skip
        log = f"{IN_RUNNER_STATE}/serve.log"
        subprocess.run(
            [
                "docker",
                "exec",
                "-d",
                self.runner,
                "sh",
                "-c",
                f'exec "$@" > {log} 2>&1',
                "sh",
                *serve,
            ],
            check=True,
        )
        print(
            self.style(
                "   $ sqlguard serve --catalog lakehouse --store actions.db --policy ...", "33"
            )
        )
        signin = self.wait_for_link(log)
        ui = reviewer.Reviewer(signin)
        deadline = time.monotonic() + 60
        while True:
            try:
                ui.sign_in()
                break
            except (OSError, reviewer.UiError) as err:
                if time.monotonic() > deadline:
                    raise StepFailed(f"cannot sign in to the approval UI: {err}") from err
                time.sleep(1)
        self.ui = ui
        self.show(
            f"approval UI at {self.review_url} (loopback only); its one-time link is not shown"
        )

    def read_in_runner(self, path: str) -> str | None:
        """A file of the runner's /state, read through `docker exec` (root owns what it wrote)."""
        done = subprocess.run(
            ["docker", "exec", self.runner, "cat", path], capture_output=True, text=True
        )
        return done.stdout if done.returncode == 0 else None

    def wait_for_link(self, log: str) -> reviewer.SignIn:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            text = self.read_in_runner(log)
            if text:
                try:
                    return reviewer.parse_signin_link(text)
                except ValueError:
                    pass
            time.sleep(0.5)
        raise StepFailed("`sqlguard serve` did not print a sign-in link")

    def agent_turn(self, calls: list[dict], prompt: str) -> None:
        """One turn of the agent: scripted MCP calls, or Claude Code given `prompt`."""
        if self.agent == "scripted":
            args = json.dumps(mcp_server_args(self.review_url))
            self.run(
                ["python", f"{DEMO}/agent.py", json.dumps(calls)],
                env={"SQLGUARD_MCP_ARGS": args},
                echo="agent.py  # a scripted MCP client; it starts sqlguard-mcp like an IDE does",
                keep=lambda line: not line.startswith("sqlguard.mcp:"),
            )
            return
        config = self.state / "claude-mcp.json"
        config.write_text(
            json.dumps(claude_mcp_config(self.runner, mcp_server_args(self.review_url))),
            encoding="utf-8",
        )
        config.chmod(0o600)
        command = claude_command(prompt, config)
        print(self.style(f"   $ claude -p {shlex.quote(prompt)[:90]}... (MCP tools only)", "33"))
        done = subprocess.run(command, text=True, capture_output=True, timeout=600)
        for line in done.stdout.splitlines():
            self.show(f"claude> {line}")
        if done.returncode != 0:
            raise StepFailed(f"claude exited {done.returncode}: {done.stderr.strip()[:300]}")

    def pending(self) -> str:
        assert self.ui is not None
        waiting = [row.action_id for row in self.ui.queue() if row.state == "pending"]
        if len(waiting) != 1:
            raise StepFailed(f"expected one pending action in the queue, found {len(waiting)}")
        return waiting[0]

    def shoot(self, location: str, name: str) -> list[str]:
        """Screenshot `location` in a headless browser signed in with the reviewer's session.

        Returns the notices the page showed. Only used with --screenshots.
        """
        assert self.ui is not None and self.shots is not None
        self.shots.mkdir(parents=True, exist_ok=True)
        job = {
            "origin": self.ui.origin,
            "base": self.ui.base,
            "cookie": [self.ui.cookie_name, self.ui.cookie_value],
            "path": location,
            "out": str(self.shots / name),
        }
        command = ["uv", "run", "--no-project", "--with", "playwright", "python"]
        done = subprocess.run(
            [*command, str(HERE / "shots.py"), json.dumps(job)],
            text=True,
            capture_output=True,
            timeout=300,
        )
        if done.returncode != 0:
            raise StepFailed(f"screenshot failed: {done.stderr.strip()[-400:]}")
        self.show(f"screenshot saved: {self.shots / name}")
        return json.loads(done.stdout.strip().splitlines()[-1])

    def review(
        self,
        action_id: str,
        decision: str,
        note: str,
        *,
        shot_before: str | None = None,
        shot_after: str | None = None,
        before_decide: Callable[[], None] | None = None,
    ) -> reviewer.ActionPage:
        """Open the action in the UI, show what the reviewer sees, decide, report the notice."""
        assert self.ui is not None
        page = self.ui.page(action_id)
        for line in page_summary(page):
            self.show(f"reviewer UI> {line}")
        if shot_before and self.shots:
            self.shoot(f"/a/{action_id}", shot_before)
        if before_decide:
            before_decide()
        self.show(f"reviewer UI> {decision} - note: {note}")
        location = self.ui.decide(page, decision, note)
        if shot_after and self.shots:
            notices = self.shoot(location[len(self.ui.base) :], shot_after)
        else:
            notices = self.ui.flashes_at(location)
        for notice in [n for n in notices if n not in page.reasons][:2]:
            self.show(f"reviewer UI> {notice}")
        return self.ui.page(action_id)

    def safety(self) -> None:
        assert self.ui is not None
        status = lambda aid: [{"tool": "get_action_status", "args": {"action_id": aid}}]  # noqa: E731
        initial = self.table_state(quiet=True)

        self.headline("An agent asks, over MCP, to delete production data")
        if self.agent == "claude":
            self.say(
                "The agent is Claude Code (headless), on the host: only the guard's MCP tools."
            )
        else:
            self.say(
                "The agent is a scripted MCP client (no model, no key): same tools, same answers."
            )
        self.agent_turn(
            [
                {"tool": "list_tables"},
                {"tool": "describe_table", "args": {"table": "prod.orders"}},
                {"tool": "run_sql", "args": {"sql": WIDE_DELETE}, "expect": "pending_approval"},
            ],
            "You look after prod.orders. Delete the cancelled orders with your tools. If the "
            "action is held for approval, report its action_id and stop; do not work around it.",
        )
        state = self.table_state()
        assert state["rows"] == initial["rows"] and state["snapshot"] == initial["snapshot"], state
        self.say("Held: main is untouched. The change was rehearsed on an Iceberg branch (above);")
        self.say("the UI shows the diff between main (base) and that branch (head).")
        self.run(
            ["sqlguard", "approvals", "list", "--store", f"{IN_RUNNER_STATE}/actions.db"],
            echo="sqlguard approvals list --store actions.db   # the same queue, in a terminal",
        )

        self.headline("The reviewer opens the approval UI, sees the diff, rejects")
        first = self.pending()
        page = self.review(
            first,
            "reject",
            "Deletes 30 orders; the agent dropped the customer filter.",
            shot_before="approval-ui-pending.png",
        )
        assert page.state == "rejected", page.state
        self.agent_turn(
            status(first), f"Check action {first} with get_action_status and report in one line."
        )
        state = self.table_state()
        assert state["rows"] == initial["rows"] and state["snapshot"] == initial["snapshot"], state

        self.headline("A legitimate request: approved, then published by snapshot id")
        self.agent_turn(
            [{"tool": "run_sql", "args": {"sql": NARROW_DELETE}, "expect": "pending_approval"}],
            "Delete only the cancelled orders of customer 1 with id up to 30 (three rows). If the "
            "action is held for approval, report its action_id and stop.",
        )
        second = self.pending()
        page = self.review(second, "approve", "Three cancelled orders of one customer. Fine.")
        assert page.state == "published", page.state
        state = self.table_state()
        assert state["rows"] == 97 and state["snapshot"] == page.head_snapshot, (state, page)
        self.show(
            f"main moved from {page.base_snapshot} to {state['snapshot']}: exactly the snapshot "
            "the reviewer saw (a compare-and-swap on the catalog)"
        )
        published = str(state["snapshot"])
        self.agent_turn(
            status(second), f"Check action {second} with get_action_status and report in one line."
        )

        self.headline("Someone else writes between the review and the click: the action goes stale")
        self.agent_turn(
            [{"tool": "run_sql", "args": {"sql": UPDATE}, "expect": "pending_approval"}],
            "Set status to 'refunded' for order 7 in prod.orders. If the action is held for "
            "approval, report its action_id and stop.",
        )
        third = self.pending()

        def other_writer() -> None:
            self.say(
                "Between the reviewer's look and the click, another job writes to prod.orders:"
            )
            self.run(["python", f"{DEMO}/lake.py", "foreign"])

        page = self.review(
            third,
            "approve",
            "One row, looks right.",
            before_decide=other_writer,
            shot_after="approval-ui-stale.png",
        )
        assert page.state == "stale", page.state
        self.agent_turn(
            status(third), f"Check action {third} with get_action_status and report in one line."
        )
        state = self.table_state()
        assert state["rows"] == 107, state

        self.headline("Rollback: undo the stray write, through the same approval")
        self.agent_turn(
            [
                {
                    "tool": "rollback",
                    "args": {"table": "prod.orders", "snapshot_id": int(published)},
                    "expect": "pending_approval",
                }
            ],
            f"Roll prod.orders back to snapshot {published}, the state before the stray write. "
            "If the action is held for approval, report its action_id and stop.",
        )
        fourth = self.pending()
        page = self.review(fourth, "approve", "Undo the stray import; keep the approved delete.")
        assert page.state == "published", page.state
        state = self.table_state()
        assert state["rows"] == 97 and str(state["snapshot"]) == published, state

        self.headline("The audit trail")
        self.say(
            "Every submission and decision, including who approved, is in the append-only log."
        )
        records = [
            json.loads(line)
            for line in (self.read_in_runner(f"{IN_RUNNER_STATE}/audit.jsonl") or "").splitlines()
        ]
        for line in audit_lines(records):
            self.show(line)
        leftovers = self.table_state(quiet=True)
        self.say(f"prod.orders: {leftovers['rows']} rows; every audit branch is gone.")

    def finish(self) -> None:
        took = elapsed(time.monotonic() - self.started)
        print(
            f"\n{self.style(f'Demo finished in {took}. Cleaning up the stack...', '1;32')}",
            flush=True,
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runner", required=True, help="name of the running runner container")
    parser.add_argument("--state", required=True, help="host directory mounted at /state")
    parser.add_argument("--agent", choices=("scripted", "claude"), default="scripted")
    parser.add_argument("--screenshots", help="directory for screenshots of the approval UI")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    story = Story(parse_args(argv))
    try:
        story.leakage()
        story.start_ui()
        story.safety()
    except StepFailed as err:
        print(f"\ndemo failed: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (AssertionError, reviewer.UiError, OSError, ValueError) as err:
        print(f"\ndemo failed: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    story.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
