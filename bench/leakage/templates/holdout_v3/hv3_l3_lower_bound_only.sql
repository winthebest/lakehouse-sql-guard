-- leak: L3
-- param w: 7 | 30
-- param lower_col: e.event_ts | e.ingested_at
-- "Recent activity": only a lower edge; nothing stops rows after t.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.event_id) AS n_recent,
       SUM(e.amount)     AS spend_recent
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND {lower_col} >= l.label_ts - INTERVAL {w} DAY
GROUP BY ALL
