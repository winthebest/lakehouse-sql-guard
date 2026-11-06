-- leak: clean
SELECT DISTINCT ON (l.user_id, l.label_ts)
       l.user_id, l.label_ts, l.churned,
       e.amount   AS last_known_amount,
       e.event_ts AS last_known_purchase_ts
FROM labels l
LEFT JOIN staging.stg_events e
       ON e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
ORDER BY l.user_id, l.label_ts, e.event_ts DESC NULLS LAST, e.event_id DESC NULLS LAST
