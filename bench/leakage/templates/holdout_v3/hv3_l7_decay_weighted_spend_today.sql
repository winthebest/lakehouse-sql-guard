-- leak: L7
-- param tau: 7 | 30
SELECT l.user_id, l.label_ts, l.churned,
       SUM(e.amount * exp(-date_diff('day', CAST(e.event_ts AS DATE), today()) / {tau}.0)) AS decayed_spend
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
GROUP BY ALL
