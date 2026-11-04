-- leak: clean
-- param k: 1 | 7
-- Clean: LAG runs over the stats snapshots visible before the prediction, and only the newest visible snapshot is kept.
WITH vis AS (
  SELECT sp.user_id, sp.label_ts, sp.churned, st.updated_at, st.total_spend,
         LAG(st.total_spend, {k}) OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY st.updated_at) AS prev_spend,
         ROW_NUMBER() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY st.updated_at DESC) AS rn
  FROM labels AS sp
  JOIN user_stats AS st ON st.user_id = sp.user_id AND st.updated_at < sp.label_ts
)
SELECT user_id, label_ts, churned, total_spend - prev_spend AS spend_growth, updated_at AS stats_ts
FROM vis
WHERE rn = 1
