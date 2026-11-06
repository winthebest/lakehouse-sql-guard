-- leak: clean
-- param w: 14 | 30
WITH activity AS (
    SELECT user_id, event_ts AS act_ts, ingested_at AS known_at, 'purchase' AS kind FROM events
    UNION ALL
    SELECT user_id, valid_from AS act_ts, ingested_at AS known_at, 'plan_change' AS kind FROM plan_history
)
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(*) FILTER (WHERE a.kind = 'purchase')    AS n_purchases,
       COUNT(*) FILTER (WHERE a.kind = 'plan_change') AS n_plan_changes,
       MAX(a.act_ts)                                  AS last_activity_ts
FROM labels l
LEFT JOIN activity a
       ON a.user_id = l.user_id
      AND a.known_at < l.label_ts
      AND a.act_ts >= l.label_ts - INTERVAL {w} DAY
GROUP BY ALL
