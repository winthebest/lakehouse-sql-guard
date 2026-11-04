-- leak: L6
-- param expr: CASE WHEN o.user_id IS NULL THEN 0 ELSE 1 END | CAST(o.user_id IS NOT NULL AS INT)
SELECT l.user_id, l.label_ts, l.churned, {expr} AS has_followup
FROM labels l
LEFT JOIN user_outcomes o ON o.user_id = l.user_id AND o.label_ts = l.label_ts
