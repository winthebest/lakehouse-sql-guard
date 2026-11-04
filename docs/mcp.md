# MCP server for IDE agents

`sqlguard-mcp` puts the guard between an AI agent in your IDE (Claude Code, Cursor, Claude
Desktop) and an Iceberg lakehouse. The agent gets `list_tables`, `describe_table`, `run_sql`,
`expire_snapshots`, `remove_orphan_files`, `rollback`, `get_action_status` and
`check_training_sql`. Every change goes through `Guard.submit`: it is classified, checked
against the policy, rehearsed on an Iceberg branch, measured, and published only if the policy
and the data-quality rules allow it. There is no unguarded tool and no tool that approves
anything.

```
agent ──MCP──▶ sqlguard-mcp ──▶ Guard ──▶ catalog
                   │              │ rehearses on a branch, saves the action
                   │              ▼
                   │         action store (SQLite)  ◀── sqlguard serve / sqlguard approvals
                   ▼                                     (you, in your own terminal)
        pending_approval + action_id
```

When the policy says "ask", the server rehearses the change, saves it in the action store
(`--store`) and tells the agent it is `pending_approval`. Nothing has changed. You review the
measured row diff and decide with `sqlguard serve` (web UI) or `sqlguard approvals` (terminal).
The agent can look the action up with `get_action_status`, but this process has no approver and
no proof verifier, so it cannot decide anything.

## Install

Requires `uv` and a policy file. Copy `config/policy.v2.yaml` to somewhere the agent cannot edit
(see [Protecting the approval](#protecting-the-approval)).

Claude Code, project scope (writes `.mcp.json`; Claude Code asks you to approve it once):

```bash
claude mcp add --transport stdio --scope project lakehouse-guard -- \
  uvx --from 'lakehouse-sql-guard[mcp] @ git+https://github.com/winthebest/lakehouse-sql-guard@v0.2.0' \
  sqlguard-mcp --catalog lake --namespace sales --scope-namespace sales \
  --policy "$HOME/.config/lakehouse-guard/policy.yaml" \
  --audit-log "$HOME/.local/state/lakehouse-guard/audit.jsonl" \
  --store "$HOME/.local/state/lakehouse-guard/actions.db" \
  --review-url http://127.0.0.1:8765 --requester claude-code
```

Ready-made files in [`examples/mcp/`](../examples/mcp):

| File | Goes to | Notes |
|---|---|---|
| `claude-code.mcp.json` | `.mcp.json` in the project | uses `${CLAUDE_PROJECT_DIR:-.}` and `${HOME}`; catalog settings through `env` |
| `claude-code.settings.json` | `.claude/settings.json` (or your user settings) | `permissions.deny` for the approval tools, the store and the credentials |
| `cursor.mcp.json` | `.cursor/mcp.json` or `~/.cursor/mcp.json` | `${workspaceFolder}`, `${userHome}`, `${env:NAME}` |
| `claude_desktop_config.json` | the Claude Desktop config file | no variable expansion: write absolute paths |

The `@v0.2.0` tag is the first release that has the `pending_approval` flow. The package name,
the `[mcp]` extra and the `sqlguard-mcp` entry point are the ones in `pyproject.toml`
(`tests/test_mcp_examples.py` checks every command and file in this document against them).

For a local warehouse instead of a catalog, replace `--catalog lake --namespace sales` with
`--warehouse /path/to/warehouse --namespace db`; the store then defaults to
`<warehouse>/sqlguard-actions.db`.

## What the agent sees

A write the policy lets through is rehearsed, verified and published; the result has
`status: "ok"`. A write the policy forbids is `blocked` with the reasons. A write that needs a
person is held:

```json
{
  "status": "pending_approval",
  "outcome": "awaiting_approval",
  "action_id": "3f6c0d1a9b7e4c58a2d4e6f81b0c9a37",
  "action_hash_prefix": "9c1e4a",
  "review_url": "http://127.0.0.1:8765",
  "expires_at": "2026-11-05T09:12:44+00:00",
  "summary": "db.orders: 0 rows added, 200 deleted (of 200)",
  "message": "Not applied: the change was rehearsed and is waiting for a reviewer's approval; ...",
  "reasons": ["DELETE without WHERE clause touches every row"],
  "impact": [{"table": "db.orders", "rows_before": 200, "rows_after": 0, "rows_deleted": 200}]
}
```

`get_action_status(action_id)` returns `state` (`pending`, `publishing`, `published`,
`rejected`, `expired`, `stale`, `failed`, `needs_attention`), the same summary and a one-line
message. It shows only actions this server queued (its `--requester` label). It never returns the
statement text.

Results are bounded: `status`, `action_id` and `summary` come first, `reasons` are deduplicated and
cut to 10 (then "... and N more reasons"), and any result over about 40,000 characters is cut to its
essentials with a note, so a host limit such as Claude Code's 25,000 tokens cannot hide the
`action_id`. Statuses: `ok`, `blocked`, `pending_approval`, `approval_rejected`, `error`,
`invalid_arguments`. A publish that was attempted but whose result could not be established
(the catalog timed out, or main holds a snapshot other than the approved one) is reported as
`blocked` with outcome `publish_unconfirmed`; `published` is `null` (unknown) there, in the audit
log too, and `guard.needs_attention` is `true`: the table may or may not hold the change, nothing was undone, and
a person has to look.

`review_url` is only the address you passed with `--review-url`, and the server refuses anything
but `scheme://host:port`. `sqlguard serve` puts the web UI under a secret path and prints the real
link, once, in your terminal; the agent never gets it.
`action_hash_prefix` is the first 6 characters of the hash the review screen shows, so you can
check that the action you are looking at is the one the agent announced.

## Server options

| Flag | Environment | Meaning |
|---|---|---|
| `--warehouse DIR` / `--catalog NAME` | `SQLGUARD_WAREHOUSE` / `SQLGUARD_CATALOG` | exactly one. A catalog is a PyIceberg catalog name |
| `--namespace NS` | `SQLGUARD_NAMESPACE` | default namespace for bare table names |
| `--policy FILE` | `SQLGUARD_POLICY` | policy YAML; use the same file for `serve` |
| `--audit-log FILE` | `SQLGUARD_AUDIT_LOG` | required with `--catalog` |
| `--store FILE` | `SQLGUARD_STORE` | action store; required with `--catalog` |
| `--review-url URL` | `SQLGUARD_REVIEW_URL` | `scheme://host[:port]` of the review UI on this machine (127.0.0.1, localhost, ::1); echoed to the agent in `pending_approval` results. A path, query or fragment is refused: never paste the link `serve` prints |
| `--requester LABEL` | `SQLGUARD_REQUESTER` | fixed label (default `mcp`) on every queued action. At most 10 actions per label (50 in all) can wait; the label is set here, not by the agent |
| `--scope-table`, `--scope-namespace` | `SQLGUARD_SCOPE_TABLES`, `SQLGUARD_SCOPE_NAMESPACES` | what the task may touch |
| `--spec-dir DIR` | `SQLGUARD_SPEC_DIR` | where `check_training_sql` finds leakage specs |

At startup the server settles publishes a crash left half-way (`Guard.reconcile`) and expires
actions nobody decided (`Guard.expire_pending`). `sqlguard serve` repeats both every minute.

## Policy v2

`config/policy.example.yaml` (v1) is unchanged. `config/policy.v2.yaml` adds:

- `environments`: namespace to `dev` or `prod`, matched without regard to case. **Every write to a
  `prod` namespace is `ask`, whatever its size**: a one-row `UPDATE prod.orders ...` is held,
  where v1 would publish it after the branch check. Reads are unaffected. **A namespace that is
  not listed counts as `prod`**, so one added later fails closed; set `default_environment: dev`
  to say otherwise. A policy with no `environments` at all behaves as v1.
- `require_table_key: true`: an `UPDATE` or `MERGE` on a table with neither Iceberg identifier
  fields nor an `invariants.tables.<t>.unique_key` is held with the reason `unkeyed diff`,
  because the reviewer would see each updated row as one deleted and one added row. Declare the
  keys of your tables to keep small updates automatic.

## Approving

Start the approval tools yourself, in a terminal the agent cannot read, with the same store,
policy, catalog and default namespace as the server:

```bash
sqlguard approvals list --store "$HOME/.local/state/lakehouse-guard/actions.db"
sqlguard serve --catalog lake --catalog-namespace sales \
  --store "$HOME/.local/state/lakehouse-guard/actions.db" \
  --policy "$HOME/.config/lakehouse-guard/policy.yaml" \
  --audit-log "$HOME/.local/state/lakehouse-guard/audit.jsonl"
```

`serve` prints a one-time link; open it once. An action decided under a different policy,
catalog or engine ends `stale`. See [architecture](architecture.md) and the approval limits in
[limitations](limitations.md).

## Protecting the approval

An agent in your IDE runs as **your OS user** and usually has a shell. The guard can only stop
what goes through MCP; whether the agent can approve its own action is decided by the host.

**Real barriers**, in order of importance. Each holds against an agent with a shell only if the
host enforces it at the operating-system level (a sandbox, another OS user, another machine) or
the agent has no shell at all:

1. **The agent's shell has no catalog credentials.** They reach the MCP process through its
   `env`, and nowhere else: not in `~/.pyiceberg.yaml`, a `.env`, shell history or the shell's
   own environment. Without them nothing the agent runs can publish: `sqlguard approvals`,
   `sqlguard serve`, `Guard(...)` and `ReviewService(...)` all need the catalog.
2. **The agent has no write access to the action store, the audit log or the policy, and no read
   access to the store.** The store holds the SQL of pending actions while they wait.
3. **The agent cannot read the browser's profile or cookie store, or the terminal where
   `sqlguard serve` runs.** The serve link and the session cookie are your credentials for the
   web UI; the URL prefix in them is secret. (This is also why `--review-url` accepts only
   `scheme://host:port` and refuses a path: the agent is given the address, never the link.)
4. Otherwise: **no shell at all for the agent, or a sandboxed shell, another OS user, or another
   machine.** Anything running as the same user with the same environment as you is outside
   what this tool can defend.

**Speed bumps**, useful against accidents and naive prompt injection, **not barriers**: the CLI's
TTY check (`script -q /dev/null ...`, `python -c 'import pty; ...'` or `expect` defeat it);
retyping the first 8 characters of the action hash (`approve` prints the hash right before
asking, and `show` and `list --json` print it with no TTY); the HMAC proof (its key is in the
memory of the process you run, but a process that has the credentials can mint its own); the
Python API. The web UI also binds to loopback, checks `Host` and `Origin`, requires a CSRF token,
serves everything under a secret random path with a cookie scoped to it, and shows a diff
recomputed from the snapshots.

### Claude Code: the sandbox is the control, the deny rules are not

`claude-code.settings.json` has two parts, and they are not equal.

**`sandbox`** (OS-level, for the Bash tool; keys checked against the Claude Code sandboxing and
settings pages): `enabled`, `failIfUnavailable` (no silent fallback to an unsandboxed shell),
`allowUnsandboxedCommands: false` (no retry outside the sandbox),
`credentials.envVars` with `mode: deny` for every variable `.mcp.json` expands from your
environment, `credentials.files` with `mode: deny` for `~/.pyiceberg.yaml`, `~/.claude.json` and
the state and config directories, and `filesystem.denyRead`/`denyWrite` for the browser profiles
and the policy and store. The point of the `envVars` entries: `${VAR}` in `.mcp.json` is read from
the environment of the process that starts Claude Code, and Claude Code's Bash tool inherits that
environment, so without them `printenv` hands the agent the credential. `tests/test_mcp_examples.py`
fails if `.mcp.json` expands a variable the sandbox does not unset.

What the sandbox does not cover, per the same documentation: the Read, Edit and Write tools (they
follow permission rules), and local MCP servers, hooks and other host processes, which run outside
it with your full access. That last point is why the credentials can live in the MCP server's
environment and still be out of the agent's reach. Any command you list in `excludedCommands`, or
a session started without the sandbox, is back to an unprotected shell.

**`permissions.deny`** (defence in depth): Read/Edit rules on the store, audit log, policy,
`~/.pyiceberg.yaml`, `~/.claude.json`, `.env` files and browser profiles, and Bash prefixes for
`sqlite3`, `duckdb`, every spelling of `sqlguard` and `uvx`, `script`/`expect`/`unbuffer`, and the
shell wrappers (`bash -c`, `sh -c`, `zsh -c`, `env`, `python -c`). **These are not OS-level.** They
cover Claude Code's own file tools and the commands it recognises, not a script the agent writes
and runs (`python3 x.py` reads any file you can), and a command prefix does not match `uv run
sqlguard ...`, `bash -c '...'` or `env sqlguard ...` spelled another way. Treat them as a nudge.

If you cannot use the sandbox, run the agent as another OS user, or without a shell.

Where the credential comes from: keep it out of the shell that starts the IDE if you can. If it
must be exported there, the sandbox's `envVars` entries are what hide it. Or store it in a
user-scope entry (`claude mcp add --scope user -e PYICEBERG_CATALOG__LAKE__CREDENTIAL=...`, kept
in `~/.claude.json`, which the settings above deny). The `claude mcp add` command in this document
sets no `-e` on purpose: pass the catalog settings the way you prefer, and keep them out of a
committed `.mcp.json`.

Place the store, audit log and policy outside the project (the examples use
`~/.local/state/lakehouse-guard/` and `~/.config/lakehouse-guard/`), so a project checkout never
contains them. Cursor and Claude Desktop have no `permissions.deny` and no sandbox setting that
we know of: rely on barriers 1 to 4. Claude Desktop often does not have `~/.local/bin` on its
`PATH`; if `uvx` is not found, put its absolute path (`which uvx`) in `command`.

## Limits

- The default executor rehearses a write by loading the whole table into memory; do not point
  `--catalog` at a large production table.
- The approver name is a label, not authentication. There is no multi-user approval.
- `check_training_sql` only reads specs under `--spec-dir`.
