-- leak: L1
-- param d: 3 | 7 | 14
-- Leaky: the second ASOF join looks d days past the prediction time, so the delta mixes in a stats snapshot from the future.
SELECT sp.user_id, sp.label_ts, sp.churned, fut.total_spend - cur.total_spend AS spend_next_{d}d
FROM labels AS sp
ASOF LEFT JOIN user_stats AS cur ON sp.user_id = cur.user_id AND sp.label_ts > cur.updated_at
ASOF LEFT JOIN user_stats AS fut ON sp.user_id = fut.user_id AND sp.label_ts + INTERVAL '{d} days' >= fut.updated_at
