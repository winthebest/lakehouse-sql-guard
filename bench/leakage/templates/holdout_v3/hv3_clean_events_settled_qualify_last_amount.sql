-- leak: clean
-- param gap: 8 | 15
SELECT l.user_id, l.label_ts, l.churned,
       e.amount   AS last_settled_amount,
       e.event_ts AS last_settled_ts
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.event_ts < l.label_ts - INTERVAL {gap} DAY
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts
                           ORDER BY e.event_ts DESC NULLS LAST, e.event_id DESC NULLS LAST) = 1
