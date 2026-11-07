# The time spec

The leakage checker never infers time from column names. You describe it in a small YAML file,
passed with `--spec`. In a dbt project the same facts can live in model `meta` instead (see
[dbt.md](dbt.md#declare-time-in-meta)).

## Fields

```yaml
label_table: churn_labels
entity_keys: [customer_id]
prediction_ts: cutoff_ts
label_columns: [churned]
time_columns: {orders: order_ts, support_tickets: opened_at}
availability_columns: {orders: loaded_at}      # when rows landed in the lakehouse
static_tables: [customers]
outcome_columns: [refunds.refund_amount]       # only known after the outcome
```

## Optional fields

Optional fields settle judgment calls the SQL alone cannot. Without them the strictest
reading applies; [src/sqlguard/leakage/spec.py](../src/sqlguard/leakage/spec.py) is the schema:

```yaml
boundary: strict                  # inclusive: a fact stamped exactly at prediction_ts is already known
max_ingest_delay: {orders: 8 days}   # longest a row takes to land after order_ts
label_known_after: 14 days        # a label is final this long after its own prediction_ts
relations: {analytics.main.stg_orders: orders}   # SQL name -> catalog table, for replay
```

- `max_ingest_delay`: an event-time bound at least that far before `prediction_ts` counts as
  an availability bound, so it no longer raises the L4 hint. Durations use seconds to weeks.
- `label_known_after`: labels of earlier rows old enough to be final may be aggregated
  (target encoding) without L6. Both are judged against `boundary`: strictly before
  `prediction_ts` unless `boundary: inclusive`.
- `boundary: inclusive` also removes L5 for `<= prediction_ts` and pins replay at
  `prediction_ts` instead of just before it.
- `relations`: maps qualified SQL names (dbt's database and schema are not Iceberg
  namespaces) to catalog tables. Static analysis and replay both apply it.
