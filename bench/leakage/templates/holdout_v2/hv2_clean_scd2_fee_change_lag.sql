-- leak: clean
-- param cmp: > | <
-- Within the versions loaded before t, LAG over valid_from gives the previous known version
-- of the newest known one. Both rows are known at t; valid_to is never read.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM labels
),
known_versions AS (
    SELECT sp.user_id, sp.label_ts, p.plan, p.monthly_fee,
           LAG(p.monthly_fee) OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY p.valid_from) AS prev_fee,
           LAG(p.plan)        OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY p.valid_from) AS prev_plan
    FROM spine sp
    JOIN snapshots.plan_history_snapshot p
      ON p.user_id = sp.user_id
     AND p.ingested_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY p.valid_from DESC) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       k.monthly_fee - k.prev_fee           AS fee_change_last_switch,
       COALESCE(k.monthly_fee {cmp} k.prev_fee, FALSE) AS last_switch_flag,
       k.prev_plan || '->' || k.plan        AS last_switch_path
FROM spine sp
LEFT JOIN known_versions k
  ON k.user_id = sp.user_id AND k.label_ts = sp.label_ts
