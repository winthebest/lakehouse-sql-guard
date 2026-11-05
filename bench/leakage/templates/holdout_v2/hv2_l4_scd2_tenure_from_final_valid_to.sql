-- leak: L4
-- param unit: day | hour
-- The version is chosen correctly (newest loaded before t), but its final valid_to is used as
-- a value. At t that valid_to was set only if the successor was already loaded.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
),
known AS (
    SELECT sp.user_id, sp.label_ts, p.plan, p.valid_from, p.valid_to
    FROM spine sp
    JOIN snapshots.plan_history_snapshot p
      ON p.user_id = sp.user_id
     AND p.ingested_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY p.valid_from DESC) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       k.plan,
       date_diff('{unit}', k.valid_from, COALESCE(k.valid_to, sp.label_ts)) AS plan_tenure_{unit}s,
       COALESCE(k.valid_to < sp.label_ts, FALSE) AS plan_already_replaced
FROM spine sp
LEFT JOIN known k
  ON k.user_id = sp.user_id AND k.label_ts = sp.label_ts
