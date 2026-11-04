-- leak: clean
-- param join: JOIN | LEFT JOIN
SELECT l.user_id, l.label_ts, l.churned, s.total_spend AS spend_asof
FROM labels l
{join} user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.updated_at DESC) = 1
