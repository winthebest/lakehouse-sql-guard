# Architecture

`lakehouse-sql-guard` checks SQL **before it runs** against an Apache Iceberg lakehouse.
Two modules share one SQL-analysis core:

- **Leakage**: is this training-set query using data that was not available at
  prediction time?
- **Safety**: may an AI agent run this action, and if so, how (directly, through a branch
  rehearsal, after human approval, or not at all)?

```mermaid
flowchart LR
    subgraph core["sqlguard.core"]
        P[parsing<br/>sqlglot] --> Q[lineage<br/>qualify + column lineage]
        C[catalog<br/>PyIceberg snapshots] --> D[duck<br/>sandboxed DuckDB]
    end
    subgraph leakage["sqlguard.leakage"]
        S[LeakageSpec YAML] --> A[analysis<br/>time bounds per table use]
        A --> R[rules L1-L6]
        RP[replay<br/>pinned snapshots] --> V[validator<br/>score + verdict]
        R --> V
    end
    subgraph safety["sqlguard.safety"]
        CL[classifier<br/>reversibility class] --> PO[policy<br/>decision]
        PO --> G[Guard.submit<br/>branch, impact, invariants, approval, publish, audit]
    end
    Q --> A
    C --> RP
    D --> RP
    P --> CL
    C --> G
```

## Core (`src/sqlguard/core`)

| Module | Role |
|---|---|
| `parsing.py` | Parse SQL with sqlglot into statements; parse errors raise `SqlParseError`. |
| `lineage.py` | Qualify a query against a schema and compute column-level lineage. |
| `catalog.py` | `LakehouseCatalog.local` (file-based `SqlCatalog`) or `from_config` (any PyIceberg catalog: REST, Glue, SQL, configured by `~/.pyiceberg.yaml` or `PYICEBERG_CATALOG__*`); the shared `--catalog` / `--warehouse` flags; snapshot history with a *logical* visibility time (`sqlguard.logical-ts` summary property, falling back to commit time); main-branch time travel; detection of expired history. |
| `duck.py` | In-memory DuckDB over Arrow tables. File and network access are disabled and the setting is locked, so untrusted SQL only sees the tables it is given. |
| `findings.py` | `Finding`, `Severity`, `Decision` shared by both modules. |

## Leakage (`src/sqlguard/leakage`)

**Input.** The SQL that builds a training set, plus a `LeakageSpec` that *declares* time
semantics (nothing is inferred from names): the label table and its prediction-time
column, the event-time column of each temporal table, optional ingestion-time columns,
static tables, and outcome columns that are only known after the label.

**Static analysis** (`analysis.py`). The query is qualified and split into scopes. Every
physical table use becomes a `TableRef`. Each AND-ed term of a WHERE / ON / HAVING clause
is normalized to

```
<time column of table use R>  OP  <prediction_ts of the label spine> + offset
```

through aliases, CTEs, derived tables, correlated subqueries, casts, `date_trunc`, and
dialect date arithmetic. Predicates under OR, NOT or CASE never count as bounds. A CTE
reused twice must be bounded at each use. Window functions that look ahead (`LEAD`,
`FOLLOWING`) or aggregate a whole partition are recorded separately.

**Rules** (`rules/`):

| Rule | Leak |
|---|---|
| L1 | Reads facts at or after prediction time (lower bound ≥ t, upper bound ≥ t + 1 day, look-ahead windows). |
| L2 | Joins a temporal table with no upper time bound. |
| L3 | Aggregates (or window-aggregates) a temporal table with no upper bound. |
| L4 | Bounded by event time only while the table declares ingestion time: late rows may leak. A hint, weighted below the threshold. |
| L5 | Boundary errors: inclusive `<=` at t, a forward shift under a day, or mixed tz-aware/naive comparison. |
| L6 | Features derived from the label or from outcome tables (lineage, predicates, expressions). |

**Replay** (`replay.py`). For a sample of prediction timestamps (half the busiest ones,
where batch-aligned boundary leaks concentrate, half random), every table is pinned to the
snapshot visible strictly before `t`, the label table is restricted to rows at `t`, and
the query is re-run. Any difference from the normal run means a value was not knowable at
`t`. This is what confirms L4: the SQL looks right, the data arrived late. If the needed
history was expired, replay reports `unverifiable` instead of guessing.

**Verdict** (`validator.py`). `score = 1 − Π(1 − wᵢ)` over findings and a replay
mismatch, with weights in `weights.yaml`. `leaky` when the score reaches the threshold;
`unverifiable` when the guard could not check part of the query (non-SELECT input,
undeclared table, spec/schema mismatch, lineage failure, expired history, or an L4 hint
without replay); otherwise `clean`. The guard never reports `clean` by default.

## Safety (`src/sqlguard/safety`)

**Classify** (`classifier.py`). Each statement or maintenance action gets a reversibility
class (read, reversible write, risky write, irreversible, out of scope, unknown) from its
AST and the catalog state. `EXPLAIN` of a read-only query is a read of the same tables;
`EXPLAIN` of anything else is unknown, because `EXPLAIN ANALYZE` runs the statement.

**Decide** (`policy.py`). A YAML policy maps classes and the task scope to `allow`,
`allow_via_branch`, `ask`, or `deny`. Unknown input is denied.

**Execute** (`guard.py`, `executor/`). `Guard.submit(action, task_scope, approver)`:

1. `deny` → nothing runs. `ask` without an approver → nothing runs.
2. Writes are rehearsed on an Iceberg branch; the impact (rows added, deleted, modified,
   schema changes) is measured and invariants (`max_delete_pct`, `not_null`,
   `unique_key`, ...) are checked before publishing.
3. The policy is re-applied with the measured impact; an approver sees the impact report.
4. Publishing is a compare-and-swap of main's head to the snapshot that was measured; a
   diverged main is refused (see *Publish by snapshot id*).
5. Post-publish invariants run on the published snapshot; failure rolls back, but only if
   main still points at that snapshot.
6. Every decision is written to an append-only JSONL audit log.

Maintenance (`expire_snapshots`, `remove_orphan_files`, rollback, fast-forward) is
planned as a dry run first. Expiring snapshots removes time travel; deleting orphan files
afterwards removes the data physically, so both are irreversible and need approval.
`remove_orphan_files` only deletes files under the table location that no retained
snapshot or ref references, re-checked against fresh metadata at deletion time.

### Executor capabilities

Every adapter (`executor/base.py`) carries an `engine` name and a frozen `Capabilities`:
`branch_write`, `publish`, `rollback`, `expire`, `orphan_cleanup`, `changelog`. All default
to `False`; an engine opts in. The Guard consults `branch_write` before it lets a write
near the engine: a SQL write on an engine that cannot run DML on an isolated branch is
denied up front with the reason `no isolated rehearsal on <engine>` (also when the policy
would only have asked for approval), because the rehearsal is what makes a write safe to
publish. Reads and policy denials are unaffected. An adapter without a `capabilities`
attribute is treated as capable of nothing. `DuckDBIcebergAdapter` and `SparkIcebergAdapter`
declare everything except `changelog`.

### Publish by snapshot id

`ExecutorAdapter.publish(branch, expected_head)` moves main to the snapshot `expected_head`
and nothing else. The Guard obtains it from `diff()`, which returns a `BranchHandle` whose
`head_snapshot_id` is the head it measured; the same snapshot is the one an approver is
shown. Anything written to the branch afterwards is not published, which a ref-based
fast-forward (Iceberg's `fast_forward` procedure, `fast_forward_branch`) cannot guarantee.

Steps, identical for every adapter because Spark publishes through the same PyIceberg
code against the same catalog: reload the table; main must still be the branch's base
(`BranchDiverged` otherwise); `expected_head == base` is a no-op; the base must be an
ancestor of `expected_head`; `set_current_snapshot(snapshot_id=expected_head)`, whose
commit carries the catalog's own assertion on the main ref (a writer that slips in makes
it fail with `BranchDiverged`). Then main is read back and the publish counts as landed when
main is on `expected_head` **or has it in its history** (other writers committed on top).

Nothing is ever rolled back from the publish step, because undoing it past another writer's
commit would destroy their work:

- Other writers on top, post-publish verification passes: `published`.
- Other writers on top, verification fails: the rollback's compare-and-swap refuses
  (main is no longer on the published snapshot), so the result is `rollback_blocked` with
  `needs_attention`.
- Main no longer has `expected_head` in its history (`PublishMismatch`), or the commit's
  outcome is unknown and main cannot be read back or still reads as unchanged
  (`PublishStateUnknown`): `rollback_blocked`
  with `needs_attention`, main untouched.
- An unknown commit state (`CommitStateUnknownException`, 5xx, timeout, lost connection) is
  settled by reading main back, with retries: on `expected_head` or beyond is a success;
  still at the base is *not* proof that nothing landed (a gateway timeout can precede a late
  commit), so it is an attention case too; only a rejected compare-and-swap
  (`CommitFailedException`) proves not-published. A confirmed commit response is trusted if the read-back itself fails.

On Spark the table cache is refreshed (`REFRESH TABLE`) after each such commit, since a
PyIceberg commit bypasses Iceberg's `SparkCatalog` cache. The approved fast-forward
*maintenance action* moves main to the snapshot recorded in its plan the same way.

`BranchDiff.branch` (the handle carrying the measured head) is a required field; code that
builds a `BranchDiff` itself must supply it.

A SQL catalog stores every table under the catalog name it was created with, so a
warehouse written by `LakehouseCatalog.local` (name `sqlguard`) is opened by
`from_config("sqlguard", ...)`.

### CLI registry

`sqlguard` (`src/sqlguard/cli/`) is a package: `__init__.py` owns the root parser, `main`
and the registry; each command group is a module that calls
`register(name, add_parser, handler)` when imported. `add_parser(subparsers)` builds the
group's parser and returns it; `handler(args)` runs the chosen command and returns the exit
code (`main` turns `OSError` / `ValueError` into exit code 2). `build_parser` imports the
group modules (`leakage`, `safety`, `approvals`) and mounts whatever registered, in
registration order, so a new group needs its own module and no edit to another group's.
`approvals.py` is reserved and registers nothing yet. Catalog flags come from
`core.catalog.add_catalog_args` / `catalog_from_args` and are shared by the groups that
open a lakehouse (`--catalog NAME` or `--warehouse PATH`, plus `--catalog-namespace`);
`sqlguard-mcp` takes `--catalog` / `SQLGUARD_CATALOG` as an alternative to `--warehouse`; it
keeps its own flag parsing (environment-variable defaults, `--namespace`, parse and open are
separate steps) and shares only `from_config`. Its default executor rewrites whole tables in
memory, so `--catalog` should not be pointed at a large production table yet.
`from_config` errors have URI passwords and secret-looking values removed before they are shown.

## Benchmarks (`bench/`)

- `bench/leakage`: a synthetic Iceberg world with nightly batches and late-arriving rows,
  SQL templates per leak type, and a real dataset (UCI Online Retail II) with simulated
  load delays. Detectors: a baseline (output timestamps after prediction time), static,
  replay, and combined. Thresholds are pre-registered on the dev split at FPR ≤ 5% and
  committed with the git SHA before held-out templates exist.
- `bench/safety`: tier 1 runs hand-written actions through the classifier and policy;
  tier 2 runs an LLM agent with and without the guard and scores the resulting table state.
