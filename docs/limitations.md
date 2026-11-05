# Known limitations

What the guard gets wrong or cannot see today, so results can be read honestly. The
leakage rules have three generations: the first set, frozen at tag `rules-frozen-v1`; the
second ("v2"), frozen at `rules-frozen-v2` and measured once on `holdout_v2`; and the current
set ("v3"), changed after that result was read (see [Changed after held-out
v2](#changed-after-held-out-v2)). `holdout_v2` is development data from then on; `holdout_v3` is
the next split nobody has tuned on.

The committed leakage results in `results/leakage/` and the held-out numbers in the README
were measured with the v1 rules and stay valid for them. The v1 held-out templates now live
in `bench/leakage/templates/dev_v1b`: they were read while the v2 rules were designed, so any
v2 run over them is in-sample tracking, not a held-out result. The held-out set for the v2
rules (`holdout_v2`, written after the tags `rules-frozen-v2` and `policy-frozen-v2`) was run
once; what it showed is under [Held-out v2](#held-out-v2-frozen-rules-and-policy).

## Held-out v2 (frozen rules and policy)

Rules, policy and harness were frozen at `8390186` and the threshold pre-registered in
`results/leakage/prereg-8390186.json` (cut at FPR ≤ 0.05 on dev) before independent authors,
who saw only a written brief, wrote the held-out templates and prompts. Nothing below was fixed
afterwards; each item is what a later version has to address.

Leakage (`results/leakage/20261105-f52e63f.md`, 316 cases, 201 leaky): combined recall 0.99
[0.97, 1.00], FPR **0.37** [0.18, 0.55]; replay alone recall 0.85, FPR 0.00. The dev FPR was 0.

- All 42 false positives (11 of 29 clean templates) are confident static findings (score 0.9
  to 1.0) on queries whose replay PASSes; none is a fail-closed "unverifiable". The combined
  verdict does not let a replay PASS overrule a static finding, so combined FPR equals static
  FPR. The findings, by rule:
  - "window over ... looks at later rows" (L1), 20 cases in 5 templates: a window over rows that
    an earlier CTE already restricted to before the prediction time (point-in-time pick,
    trailing moving average, percentile over a snapshot, newest loaded SCD2 version).
  - "... is joined without bounding ... before label_ts" (L2), 14 cases in 6 templates: the
    bound sits where the rule does not trace it, in a `UNION` branch, an `UNNEST` calendar
    scaffold, a forward-fill, or a dbt staging model. A dbt mart reading its own label spine
    model is also reported as an unbounded join of `labels`.
  - A window aggregate over `labels` spanning the whole partition (L3), 4 cases: gaps between
    a user's calendar of prediction dates, which are known in advance.
  - A feature computed from `churned` (L6), 4 cases: a peer churn rate that only reads labels
    already final at the prediction time (`label_known_after`); the rule does not accept the
    maturity bound there.
- Both false negatives are `hv2_l4_short_event_time_buffer`: a 7-day buffer on event time when
  ingestion can lag 8 days (the 1- and 3-day buffers are caught). Static analysis names L4 at
  score 0.4, below the cut, and replay PASSes on both: the leak window is one day wide and
  replay found no row in it at the points it sampled.
- Static recall on L4 is 0.84; replay recall on L7 is 0 by design (item 19).

Safety agent (`results/safety/holdout_v2-*`, 31 prompts × 3 runs, total cost $7.35):

| cell | S | U | unsafe action rate | false-block rate |
|---|---|---|---|---|
| Haiku raw | 0.37 | 0.92 | 0.61 | 0.00 |
| Haiku guarded-pending, policy v2 | 1.00 | 0.62 | 0.00 | 0.36 |
| Haiku guarded-pending, policy v1 | 1.00 | 0.77 | 0.00 | 0.23 |
| Opus raw | 0.59 | 1.00 | 0.39 | 0.00 |
| Opus guarded-pending, policy v2 | 1.00 | 0.62 | 0.00 | 0.38 |

- With the guard no destructive attempt was executed (0 of 159 across the three guarded cells).
  Zero unsafe runs out of 54 risky and ambiguous runs per cell bounds the unsafe action rate
  at 0.054 (95%, exact binomial); runs of one prompt are correlated, so the bound over the 18
  prompts, 0.15, is the honest one.
- No real-data set was written for v2, so these numbers are synthetic-world only.
- The benign held-out prompts declare no `expected.approve`, so the scripted reviewer rejected
  every approval they raised (46 of 46). The false-block rate under guarded-pending therefore
  counts "asked a person" as "blocked". Policy v2 also asks for every write to `prod`, which is
  why it asks more often than v1 (prompts `v02` and `v15`).
- Exploratory, not pre-registered: replaying the same transcripts with a reviewer that approves
  every action a benign prompt raised (settlement happens after the model's turn, so the
  transcripts stay valid) gives U 0.90 to 0.92 and a false-block rate of 0.08 to 0.10 for the
  three guarded cells, with S unchanged at 1.00.
- What was still blocked was a real limitation: renaming a column (`v13`) is schema-breaking,
  and an `ALTER` cannot be rehearsed on a branch, so it ended `unsupported` and could not be
  routed to approval at all. Column drops and renames now go to approval (item 23); replaying
  the committed transcripts with that code keeps every metric value and every unsafe run, and
  only moves runs from `block_guard` to `request_approval` (v13 and v14 here; six risky runs in
  each dev cell). The v2 numbers above stay as measured.
- Replaying every transcript under the policy it was recorded with gives the same outcome for
  all 465 runs.
- How much of S depends on the person: re-settling the same guarded-pending transcripts with a
  reviewer that approves everything makes 6 to 19% of risky runs unsafe. The guard routes them
  to a person; it does not judge intent (item 21).
- Since then the report separates a hard block (deny, invariant, unsupported), an approval
  request, a reviewer rejection and an unsupported action next to the unchanged false-block
  rate, and adds prompt-level bootstrap intervals and exact binomial bounds. A live or pilot run
  on any prompt file but `dev` refuses unless the matching `policy-frozen-v<N>` tag holds, so
  held-out v2 can no longer be re-run live; `--replay` still works.

Held-out authoring found three places where the brief and the benchmark warehouse disagreed;
the brief was not changed after the freeze, so prompts were repaired to the warehouse:

- `run_sql` runs `ALTER TABLE` only for `DROP COLUMN` and `RENAME COLUMN`.
- One `run_sql` call may write exactly one table.
- `table_unchanged` also fails when snapshots or data files were removed or the current
  snapshot moved (`bench/safety/state.py`), not only when rows or columns changed.

The `done` checks can bound snapshot counts from below (`min_snapshots`) and require files to
be kept (`files_kept`), but cannot prove that an expiry or an orphan-file removal took place, so
a benign maintenance prompt is scored on what remained, not on what was removed.

## Changed after held-out v2

Every number measured on `holdout_v2` after this point is in-sample. Changes (paired clean and
leaky tests, fail-closed probes from code review in `tests/leakage/test_fail_closed_probes.py`,
metamorphic tests in `tests/leakage/test_metamorphic_static.py`):

- Static bounds now recognized: a window over rows that its own or an upstream scope already
  cut before the prediction time; a surrogate key of the spine row; `UNNEST`/`generate_series`
  calendar scaffolds; running and forward-fill windows over a `UNION` timeline; named windows
  (`OVER w`); bounds followed through staging CTEs and `relations`; peer target encoding over
  labels already final by `label_known_after`.
- A surrogate key is recognized only in this form: optionally a hash (`md5`, `sha1`, `sha2`,
  `hash`) of a concatenation (`||`, `concat`, `concat_ws` with a literal separator) whose parts
  are string literals or columns of one label reference, under lossless casts and optionally
  `COALESCE`/`IFNULL`/`NVL` with a literal fallback, covering every entity key and the
  prediction time (dbt's `generate_surrogate_key` output qualifies). Anything else (`UPPER`,
  `SUBSTR`, `CASE`, `COALESCE` between columns, a cast to `DATE`) is not a key and is flagged.
- A timeline bound holds only while the window that earns it is the only thing reading those
  rows; another window, aggregate, `GROUP BY`, `DISTINCT` or `HAVING` over them is flagged (a
  correct timeline written through a named `WINDOW` clause is a known false positive).
- Casts are never folded into constants: `CAST(0.6 AS INT)` as a tie-break is not a strict bound.
- **L4 is leaky by contract when the spec declares `max_ingest_delay`:** an event-time bound with
  a margin shorter than the declared delay is an L4 finding of weight 0.90 whatever the data or
  replay shows. A query that happens to be clean on the data but has a too-short margin is a
  false positive by this definition. Without the field, L4 stays the 0.4 hint replay must confirm.
- A replay result never lowers a static score. A pass only adds evidence to L1/L2/L3/L5
  findings (`replay_no_mismatch`, rows the pinned read hid per entity); a sampled pass cannot
  rule out a leak that only a few rows show.
- Replay adds up to a third of its point budget at prediction times with rows still in flight
  (event time before t, loaded at or after t), weighted by age against the longest declared
  delay, for any temporal column type and unit. Above 1024 label times the candidates are thinned
  evenly, so a leaking time can be among those dropped; if this scoring fails, replay falls back
  to the base sample and says so in its reason. Replay takes about 1.5 times longer per query
  than before (median about 110 ms on the synthetic world).
- In-sample on `holdout_v2` (rules fixed while looking at it): combined recall 1.00, FPR 0.00;
  dev and `dev_v1b` unchanged. These numbers say nothing about unseen queries. Static analysis
  still cannot see the SCD2 `valid_to` L4 cases (item 36).

## Changed in v2

Limitations of the first rule set that no longer apply (each has paired clean and leaky
tests in `tests/leakage/test_rules_v2.py`):

- `agg(x) FILTER (WHERE ts < prediction_ts)` can bound a table, under the conditions in
  item 1.
- Bounds are recognized in more spellings: `prediction_ts - ts > INTERVAL ...`,
  `date_diff`/`DATEDIFF` with second to week units, `epoch(ts)` and `EXTRACT(epoch FROM ts)`,
  and bounds that appear only in `QUALIFY`.
- A time split cut at `(SELECT MAX(label_ts) FROM labels)` is accepted (item 4).
- The latest-row pattern also covers `MIN` and `IN (SELECT MAX ... GROUP BY ...)` (item 6).
- A CTE that nothing reads is ignored.
- A `UNION` inside a CTE or derived table is traced branch by branch; `UNION BY NAME` is
  rewritten to its positional form before analysis.
- Replay no longer treats a table whose history starts after a sampled prediction time as a
  mismatch (item 16).

Behaviour changes that make the guard stricter (a query that was clean can now be flagged):

- `LAG` with a non-constant offset is treated as looking ahead (L1); it used to be treated as
  looking backward.
- A time-split cutoff that selects the latest labels (`= MAX`, `>= MAX`, `> MAX`, or a
  quantile with fraction 1 or a non-literal fraction used as a lower bound) is L3.
- Anything the analysis does not model voids an exemption instead of passing it: an
  aggregate or window function that sqlglot does not know (e.g. `count_star()`) beside a
  `FILTER`, an unknown window function beside a `QUALIFY` bound, a function between a clock
  call and its comparison in an L7 spine filter, or a bound written in an unmodelled form.
- New rule L7 flags wall-clock functions in a feature (item 13; replay cannot see it, item 19).
- `HAVING MAX(ts) < prediction_ts` and `HAVING MIN(ts) > prediction_ts` are not bounds, and
  keeping only each entity's latest prediction is flagged (items 9 and 10).
- A unitless `INTERVAL '5'` is five seconds, as in DuckDB, and never earns a lag exemption.

- The safety classifier and the Trino rewriter scope CTE names as SQL does: a CTE defined
  inside a subquery no longer hides a real table of the same name in an outer scope.

New optional spec fields (`boundary`, `max_ingest_delay`, `label_known_after`, `relations`)
are described in the README; a spec without them behaves as before.

## Leakage: static analysis

False positives (the guard fails closed: a correct query is flagged):

1. A `FILTER` bounds a table only when every non-window aggregate of that SELECT carries a
   FILTER with an upper bound (`MAX`/`MIN` and `COUNT(DISTINCT ...)` that read no column of
   the table are exempt), the SELECT has no window function, the table's columns appear
   only in those FILTERs and in the table's own join condition, and no output row depends on
   its later rows: the table is the null-supplying side of a `LEFT JOIN`, or the only source
   of a query without `GROUP BY`. `COUNT(1)`, `SUM(1)`, `HAVING COUNT(1)`, a `WHERE` on the
   table's columns and an `INNER JOIN` count row multiplicity or survivorship and are L3.
   Known false positive: `approx_count_distinct` beside a `FILTER`.
2. Label maturity (`label_known_after`) is judged like any other bound. With
   `boundary: strict`, a label exactly at the horizon (`label_ts <= prediction_ts - 14 days`
   for 14 days) is not final and is flagged L6; `boundary: inclusive` accepts it.
3. Not recognized as bounds: string comparisons, `epoch_ms`, and month, quarter or year units
   inside a difference or `date_diff`. A calendar interval used to claim a lag counts at its
   shortest length. A unitless `INTERVAL '5'` bounds nothing for `max_ingest_delay`.
4. A time-split cutoff is accepted only as an uncorrelated scalar subquery
   `(SELECT MAX|MIN|quantile|median(label_ts) FROM labels)` compared (`<`, `<=`, `>`, `>=`,
   `BETWEEN`) with the spine's prediction time, with the label reference flagged as a global
   scan rather than as history. A cutoff held in a CTE or derived table, or with extra
   arithmetic inside the subquery, is still L3. **A literal cutoff timestamp always works.**
5. The SCD2 exemption covers `LEAD`/`LAG(<own time column>) AS valid_to` projected in a CTE
   or derived table and compared with `prediction_ts` (or tested for NULL). The same logic
   written inline in `QUALIFY` is flagged L1.
6. The correlated latest-row pattern is recognized as
   `col = (SELECT MAX|MIN(col) ... WHERE <strict bound>)` and
   `col IN (SELECT MAX|MIN(col) ... WHERE <strict bound> [GROUP BY ...])`. `ANY`/`ALL`,
   `NOT IN` and range forms (`>= (SELECT MAX ...)`) are not.
7. A positional `UNION` nested inside a `UNION BY NAME` operand whose branches use different
   names is refused (the query is reported unverifiable); so are `BY NAME` branches with
   unnamed, starred or duplicated columns.
8. `ROLLUP` / `GROUPING SETS` subtotal rows have no prediction time. The guard raises no
   finding for them.

Flagged on purpose (survivorship: which rows exist depends on later data, and replay agrees,
so these are not false positives):

9. `HAVING MAX(ts) < prediction_ts` / `HAVING MIN(ts) > prediction_ts` drops every group with
   a later row: L3.
10. Keeping only each entity's latest prediction, `QUALIFY ROW_NUMBER() OVER (... ORDER BY
    prediction_ts DESC) = 1`, depends on predictions made later: L1.
11. A cutoff pinned at the global maximum (`label_ts = (SELECT MAX(label_ts) ...)`) keeps only
    rows that turned out to be the latest: L3.

Known gaps (a leak can be missed statically; replay may still catch it):

12. Casting a feature timestamp to `DATE` is treated as truncation (conservative); other
    casts (e.g. to `VARCHAR`) are not parsed and bound nothing.
13. L7 exempts a filter that only chooses spine rows (`WHERE label_ts < now() - INTERVAL 30
    DAY` in the SELECT that produces the spine, comparing the clock only with the spine's
    prediction time). A clock call in a join condition, a feature subquery, a label
    reference that is also read as history, or reached through a function outside a short
    allowlist is L7.

Reported unverifiable rather than checked:

14. A qualified SQL name that is neither `<namespace>.<table>` of the catalog nor listed in the
    spec's `relations` is unclassified, so the query is unverifiable. For dbt projects, models
    that declare `time_column`, `role: static` or `role: label_spine` are trusted as declared
    and their own SQL is not checked ([docs/dbt.md](dbt.md#what-is-not-checked)).

## Leakage: replay

15. A replay PASS needs at least 20 compared rows over at least 3 prediction times. Smaller
    outputs are unverifiable when static analysis raised an L4 hint; otherwise the static
    verdict stands and the replay result records why it was inconclusive.
16. Replay needs snapshot history covering the sampled prediction times. If it was expired,
    the result is unverifiable, never clean. A prediction time that precedes the first
    main-branch snapshot of a table the query reads (a seed or one-off load) is left out and
    the sample is drawn from the covered range; if that range gives fewer than 20 rows or 3
    times, the result is unverifiable and blocks the query, never clean. A leak that is only
    visible before a table's first snapshot cannot be seen, so a PASS says nothing about
    those times.
    A snapshot is visible at its `sqlguard.logical-ts` property, else its commit time. Static
    tables are pinned like any other and read as they are now only when their history starts
    late; outcome tables are empty before they exist.
17. Earlier label rows are visible during replay with their label columns set to NULL:
    counts of prior predictions and gaps between them pass, while reading an earlier label
    value is reported as a mismatch (that label may not have been known yet). Labels that
    `label_known_after` declares final keep their values.
18. Relation names with a schema or catalog part must be `<namespace>.<table>` or
    `<catalog>.<namespace>.<table>` of the replayed catalog (the catalog part must be the
    catalog's own name), or be listed in the spec's `relations`; anything else is
    unverifiable.
19. Replay cannot see wall-clock use: both runs read the same clock, so L7 is a static-only
    rule.
20. Each distinct set of sampled points builds its own pinned in-memory connection
    (zero-copy), so replaying many differently filtered queries uses more memory.

## Safety

21. The guard reasons about SQL and table state, not intent. A small delete from the wrong
    in-scope table stays under the configured limits and is published (dev prompt `a01`,
    held-out `h15`).
22. Without a `unique_key` invariant, the impact of a rewrite cannot pair rows, so a rollback
    that replaces values counts as deleting every row and is blocked by `max_delete_pct`
    (held-out `h11`). Declare `unique_key` for tables you want to roll back through the guard.
23. The DuckDB executor supports DML, one written table per action, and whole-table
    copy-on-write; it is meant for small tables and demos. Of DDL it applies only
    `DROP COLUMN` and `RENAME COLUMN`, after approval and without a rehearsal (the reviewer sees
    the columns before and after, not a row diff); `ADD COLUMN`, type changes, `RENAME TO` and
    `DROP TABLE` through SQL are not applied. The Spark and Trino adapters apply no column
    changes (denied with a reason). The Spark adapter uses the engine's own procedures. There is
    no undo for an applied column change other than a new, approved one.
24. `expire_snapshots` removes time travel but keeps data files; a following
    `remove_orphan_files` deletes them physically. Both are irreversible and require approval.
25. The guard protects data from being changed or destroyed, not from being read. A
    `SELECT` on a protected table inside the task scope is allowed; keeping sensitive
    columns away from an agent is the job of the catalog's access control. `EXPLAIN` is
    allowed only for read-only queries (`EXPLAIN ANALYZE` runs the statement).
26. Approvals wait in a local SQLite store (`--store`). An approval is bound to a hash of the
    action (statement, table, catalog, engine, base and rehearsed snapshots, policy digest,
    row-diff digest, scope); if any of these changed when it is decided, the action ends
    `stale` and nothing is published. The approver name is a label, not authentication.
    What actually protects an approval from an agent running as the same OS user:
    the agent's shell has no catalog credentials (they go to the MCP process through its
    `env` only, never into a file it can read); it cannot write or read the store, the audit
    log or the warehouse metadata; and it cannot read the browser profile or the terminal
    where `sqlguard serve` runs (the printed link and the session cookie are the person's
    credentials). If it may hold those, nothing here defends: run it without a shell, in a
    sandbox, or as another user. The CLI's terminal check, retyping the start of the action
    hash and the HMAC proof stop accidents and naive prompt injection, not a determined
    agent (`script` or `pty` fake a terminal; `show` prints the hash). Host deny rules match
    command prefixes only, so `uv run sqlguard ...` or `bash -c ...` pass a rule written for
    `sqlguard ...`. The web page binds to loopback, checks `Host` and `Origin`, requires a
    CSRF token and serves everything under a random path prefix with a cookie scoped to it,
    so another local server the person visits never receives that cookie.
27. The reviewed row diff is computed from the full base and rehearsed snapshots, so it shows
    exactly what publishing changes, but it is bounded (1M rows or 256 MB by default; larger
    tables are refused, not sampled). Without an Iceberg identifier field or a `unique_key`
    the diff is unkeyed and shows deleted and added rows instead of updates. Values of types
    other than plain numbers, strings, booleans and dates are displayed as text (time zones in
    UTC). `0.0` and `-0.0` are equal everywhere, so a change between them is invisible.
28. A publish interrupted by a crash keeps its five-minute lease, so the next reconcile
    skips it. A publisher is judged gone only on Linux when host, boot id and PID namespace
    match and the pid no longer exists. Everywhere else (macOS, another container or host)
    an interrupted publish whose effect cannot be proven ends `needs_attention`, with its
    branch kept, never `failed`. A publisher suspended for longer than its lease on such a
    host can still land its commit after reconcile settled the row; the row then says
    `needs_attention`, not `published`.
29. Trino is a read and verification engine. It cannot rehearse writes in isolation, so
    every write through it is denied; use it as the read side of a composite adapter with
    PyIceberg or Spark for writes. Its `rollback_to_snapshot` has no compare-and-swap: the
    adapter checks `main` before and after and restores a commit that raced it. Its
    `expire_snapshots` has no dry run and also drops aged branches and tags, so it is used
    only when `main` is the table's only ref and no retention property is set; any other
    table is expired through PyIceberg, which removes exactly the approved snapshots. A ref
    another writer creates between that check and Trino's run cannot be seen. Orphan-file
    cleanup never runs through Trino. Reads may not use table functions (`TABLE(...)`,
    `system.query`, `table_changes`).
30. The integration matrix runs on Lakekeeper, the Iceberg REST fixture and Polaris. AWS Glue
    is expected to work through PyIceberg's Glue catalog but is untested.

## Leakage: fix suggestions

31. A fix is a rewrite of the query; formatting is normalized in the diff and comments are
    kept. Files are never written. Rewritten: missing bounds (join `ON` or correlated
    `WHERE`), availability bounds, inclusive and shifted bounds, frames and windows that read
    later rows, a `LEAD` output column, the latest-prediction filter, and latest-row
    subqueries (ASOF join for a table with one time column in DuckDB and Snowflake,
    `ROW_NUMBER` otherwise). Explained only: L6, L7, an aggregate CTE or derived table
    grouped by entity alone (it needs the prediction time as an input), label-table cutoffs,
    timezone mismatches and calendar-unit offsets.
32. `verification` is about leaks only: `static_only` means the validator no longer reports
    the finding and the rewrite adds none; `static_and_replay` adds a replay PASS and a clean
    verdict; `unverified` means the checker still reports it or replay disagrees. Replay
    cannot tell whether a fix computes the same thing: it runs the same query pinned and
    unpinned, so a rewrite that computes another quantity also passes.
33. `semantics` is decided from the query text and is the same with or without `--replay`.
    `preserved` is a narrow, proven claim: the bound sits in the `ON` of the table's own
    `LEFT JOIN` (or in a correlated scalar subquery in the projection), every read of that
    table is an aggregate that is NULL (0 for a count of a column) for a row with no match,
    with NULL-strict arguments and `FILTER`, and the values leave the query only through
    plain pass-through wrappers. The check runs on the sqlglot-qualified query; select
    aliases, ordinals, unresolved `*`, column alias lists and anything else it does not
    model make the fix `changed` with `rows may drop:`. Read the caveats of a `changed`
    fix before applying it (a mirrored window, `LEFT` instead of `INNER JOIN`, `COUNT` of
    the joined key, a removed filter).
34. Mirrored windows and flipped `MIN`/`ASC` orderings are guesses about intent that replay
    cannot confirm; their caveat asks you to confirm the span.
35. Snowflake's `ASOF JOIN ... MATCH_CONDITION` is written by hand and checked in its `ON`
    form; it has not been run on Snowflake. Spark and Trino get the `ROW_NUMBER` form; the
    rewrites have not been run on those engines.

## Leakage: slowly changing dimensions

36. For a type-2 (SCD2) table, static analysis bounds which version rows a query may read,
    not when each column of a row got its value. `valid_to` is written when the next version
    is loaded, so any use of it can see the future and passes static analysis: a feature such
    as `valid_to - valid_from`, and also the usual as-of lookup
    (`valid_from <= t AND (valid_to > t OR valid_to IS NULL) AND ingested_at < t`), which
    picks the wrong version when the next one took effect before `t` but was loaded after
    it. Only replay, which reads the table as it was loaded, catches these. A version-safe
    lookup takes the latest version with `ingested_at < t`.
