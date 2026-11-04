# Known limitations

These were frozen with the leakage rules (tag `rules-frozen-v1`) and with the safety policy
(tag `policy-frozen-v1`). They are listed so results can be read honestly; held-out
benchmark failures are analyzed against this list in `results/`.

## Leakage: static analysis

False positives (the guard fails closed: a correct query is flagged):

1. `agg(x) FILTER (WHERE ts < prediction_ts)` does not bound the table. A query that bounds
   every aggregate only through FILTER is flagged L3.
2. Bounds on an aggregate of the time column (`HAVING MAX(ingested_at) < prediction_ts`) and
   bounds that appear only in QUALIFY are not recognized.
3. Not normalized: `prediction_ts - ts > INTERVAL 0`, `DATEDIFF(...) > 0`, `epoch()`, string
   comparisons. A unitless `INTERVAL '5'` is read as days.
4. Time-split cutoffs computed from the label table
   (`WHERE label_ts < (SELECT MAX(label_ts) FROM labels)`) are flagged L3, and replay
   mismatches them too. **Use a literal cutoff timestamp for time splits.**
5. De-duplicating or ranking the spine with a DESC window whose partition omits
   `prediction_ts` is treated as look-ahead.
6. The SCD2 exemption covers `LEAD`/`LAG(<own time column>) AS valid_to` projected in a CTE
   or derived table and compared with `prediction_ts` (or tested for NULL). Inline window
   forms are flagged.
7. The correlated latest-row pattern is recognized only as
   `col = (SELECT MAX(col) ... WHERE <strict bound>)`; `MIN`, `IN (...)` forms are not.
8. A CTE that is defined but never used is still analyzed, and flagged if unbounded.
9. UNION is supported only at the query root. A UNION inside a CTE or a derived spine cannot
   be traced and the query is reported unverifiable.

Known gaps (a leak can be missed statically; replay may still catch it):

10. Window functions with a non-literal `LAG` offset are treated as looking backward.
11. Casting a feature timestamp to `DATE` is treated as truncation (conservative); other
    casts (e.g. to `VARCHAR`) are not parsed and bound nothing.

## Leakage: replay

12. A replay PASS needs at least 20 compared rows over at least 3 prediction times. Smaller
    outputs are unverifiable when static analysis raised an L4 hint; otherwise the static
    verdict stands and the replay result records why it was inconclusive.
13. Earlier label rows are visible during replay with their label columns set to NULL:
    counts of prior predictions and gaps between them pass, while reading an earlier label
    value is reported as a mismatch (that label may not have been known yet).
14. Replay needs snapshot history covering the sampled prediction times. If it was expired,
    the result is unverifiable, never clean.
15. Each distinct set of sampled points builds its own pinned in-memory connection
    (zero-copy), so replaying many differently filtered queries uses more memory.

## Safety

16. The guard reasons about SQL and table state, not intent. A small delete from the wrong
    in-scope table stays under the configured limits and is published (dev prompt `a01`,
    held-out `h15`).
17. Without a `unique_key` invariant, the impact of a rewrite cannot pair rows, so a rollback
    that replaces values counts as deleting every row and is blocked by `max_delete_pct`
    (held-out `h11`). Declare `unique_key` for tables you want to roll back through the guard.
18. The DuckDB executor supports DML only (no DDL such as `ALTER TABLE ... ADD COLUMN`), one
    written table per action, and whole-table copy-on-write; it is meant for small tables and
    demos. The Spark adapter uses the engine's own procedures.
19. `expire_snapshots` removes time travel but keeps data files; a following
    `remove_orphan_files` deletes them physically. Both are irreversible and require approval.
20. The guard protects data from being changed or destroyed, not from being read. A
    `SELECT` on a protected table inside the task scope is allowed; keeping sensitive
    columns away from an agent is the job of the catalog's access control. `EXPLAIN` is
    allowed only for read-only queries (`EXPLAIN ANALYZE` runs the statement).
