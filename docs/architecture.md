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
| `catalog.py` | Local PyIceberg `SqlCatalog`; snapshot history with a *logical* visibility time (`sqlguard.logical-ts` summary property, falling back to commit time); main-branch time travel; detection of expired history. |
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
4. Publishing is a compare-and-swap of main's head; a diverged main is refused.
5. Post-publish invariants run on the published snapshot; failure rolls back, but only if
   main still points at that snapshot.
6. Every decision is written to an append-only JSONL audit log.

Maintenance (`expire_snapshots`, `remove_orphan_files`, rollback, fast-forward) is
planned as a dry run first. Expiring snapshots removes time travel; deleting orphan files
afterwards removes the data physically, so both are irreversible and need approval.
`remove_orphan_files` only deletes files under the table location that no retained
snapshot or ref references, re-checked against fresh metadata at deletion time.

## Benchmarks (`bench/`)

- `bench/leakage`: a synthetic Iceberg world with nightly batches and late-arriving rows,
  SQL templates per leak type, and a real dataset (UCI Online Retail II) with simulated
  load delays. Detectors: a baseline (output timestamps after prediction time), static,
  replay, and combined. Thresholds are pre-registered on the dev split at FPR ≤ 5% and
  committed with the git SHA before held-out templates exist.
- `bench/safety`: tier 1 runs hand-written actions through the classifier and policy;
  tier 2 runs an LLM agent with and without the guard and scores the resulting table state.
