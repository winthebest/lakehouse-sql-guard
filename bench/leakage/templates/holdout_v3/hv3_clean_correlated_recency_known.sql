-- leak: clean
-- param col: e.event_ts | e.ingested_at
-- Recency of the last purchase that was already loaded at t (or of the last load itself).
SELECT l.user_id, l.label_ts, l.churned,
       date_diff('minute',
                 (SELECT MAX({col}) FROM events e
                   WHERE e.user_id = l.user_id AND e.ingested_at < l.label_ts),
                 l.label_ts) / 1440.0 AS days_since_last_known,
       (SELECT COUNT(*) FROM events e
         WHERE e.user_id = l.user_id AND e.ingested_at < l.label_ts) AS n_known_purchases
FROM labels l
