-- leak: clean
-- param k: 3 | 5 | 10
SELECT l.user_id, l.label_ts, l.churned, r.avg_last_k, r.max_last_k
FROM labels l
LEFT JOIN LATERAL (
    SELECT AVG(x.amount) AS avg_last_k, MAX(x.amount) AS max_last_k
    FROM (SELECT e.amount
          FROM events e
          WHERE e.user_id = l.user_id
            AND e.ingested_at < l.label_ts
          ORDER BY e.event_ts DESC, e.event_id DESC
          LIMIT {k}) x
) r ON TRUE
