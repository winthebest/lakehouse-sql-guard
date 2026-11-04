# Fix suggestions demo

Seven training-set queries over the churn schema in the parent folder. Five leak in a way the
guard can rewrite; two it can only explain.

```bash
uv run sqlguard leakage check examples/leakage/fix-demo/sql \
  --spec examples/leakage/spec.yaml --schema examples/leakage/schema.yaml --suggest-fix
```

For each leaky query you get what the fix does, the caveats (what else it changes), and one
unified diff. Formatting is normalized on both sides and **no file is modified**. `--json` adds a
`fixes` list with the full `before_sql` / `after_sql`.

| file | leak | what the fix does |
|---|---|---|
| `01_tickets_inner_join.sql` | L3 | bounds `opened_at` in the join; INNER JOIN becomes LEFT JOIN and `COUNT(*)` becomes `COUNT(t.customer_id)` so customers without tickets keep a row with 0 |
| `02_orders_event_time.sql` | L4 | adds `loaded_at < cutoff_ts` next to the event-time bound |
| `03_orders_inclusive.sql` | L5 | `loaded_at <= cutoff_ts` becomes `<` |
| `04_orders_next_30d.sql` | L1 | mirrors the forward window into the past, then adds the load-time bound |
| `05_latest_ticket_priority.sql` | L2 | "latest ticket" per cutoff: an `ASOF LEFT JOIN` (one time column; DuckDB and Snowflake), a `ROW_NUMBER` elsewhere or when the table has an event and an availability column |
| `06_spend_by_customer_cte.sql` | L3 | explained only: an aggregate grouped by customer in a CTE needs the cutoff as an input |
| `07_refund_feature.sql` | L6 | explained only: the feature does not exist at prediction time |

## Verification levels

Every fix says how far it was checked:

- `static_only`: the validator no longer reports the finding and the rewrite introduced no new
  one (what you get with `--schema`, as above).
- `static_and_replay`: also replayed over the snapshot history without a difference. Add
  `--warehouse DIR --replay` (a lakehouse with history) to get it.
- `unverified`: the rewrite did not remove the finding, replay disagrees, or there is no
  rewrite (L6, L7, entity-only CTEs).

ASOF joins are only produced for a table with a single time column. A table with an event time
and an availability time (`orders` here) always gets the two bounds in a plain join instead: one
inequality cannot hold both.
