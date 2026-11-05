-- leak: L4
-- param lookup: (p.valid_to IS NULL OR p.valid_to >= sp.label_ts) | COALESCE(p.valid_to, TIMESTAMP '9999-12-31') > sp.label_ts
-- param col: plan | monthly_fee
-- The classic SCD2 as-of lookup, even with ingested_at < t: today's valid_to closes a version
-- whose successor may have been loaded only at or after t, so the row known at t drops out.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
snap AS (
    SELECT user_id, plan, monthly_fee, valid_from, valid_to, ingested_at
    FROM analytics.snapshots.plan_history_snapshot
),
plan_at_t AS (
    SELECT sp.user_id, sp.label_ts, p.{col} AS v
    FROM spine sp
    JOIN snap p
      ON p.user_id = sp.user_id
     AND p.ingested_at < sp.label_ts
     AND p.valid_from  < sp.label_ts
     AND {lookup}
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       pa.v AS plan_{col}_at_t
FROM spine sp
LEFT JOIN plan_at_t pa
  ON pa.user_id = sp.user_id AND pa.label_ts = sp.label_ts
