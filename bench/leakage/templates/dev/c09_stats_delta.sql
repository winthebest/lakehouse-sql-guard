-- leak: clean
-- param window: 7 | 14 | 30 | 90
-- param asof: ASOF JOIN | ASOF LEFT JOIN
SELECT l.user_id, l.label_ts, l.churned, s.total_spend - p.total_spend AS spend_delta
FROM labels l
{asof} user_stats s ON l.user_id = s.user_id AND l.label_ts > s.updated_at
{asof} user_stats p ON l.user_id = p.user_id AND l.label_ts - INTERVAL {window} DAY > p.updated_at
