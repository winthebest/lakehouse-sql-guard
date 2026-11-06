-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       bool_or(p.plan <> 'free') AS ever_paid_known,
       MAX(p.monthly_fee)        AS max_fee_known,
       MIN(p.valid_from)         AS first_plan_start_known
FROM labels l
LEFT JOIN analytics.snapshots.plan_history_snapshot p
       ON p.user_id = l.user_id
      AND p.ingested_at < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
