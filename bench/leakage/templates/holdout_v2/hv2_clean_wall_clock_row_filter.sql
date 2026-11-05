-- leak: clean
-- param clock: now() | current_date | current_timestamp
-- param age: 14 | 30
-- The wall clock only selects which spine rows to train on (mature rows); the feature is
-- the newest stats snapshot stamped strictly before t.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM labels
    WHERE label_ts < {clock} - INTERVAL {age} DAY
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       (SELECT max_by(s.total_spend, s.updated_at)
        FROM user_stats s
        WHERE s.user_id = sp.user_id
          AND s.updated_at < sp.label_ts) AS spend_as_of_t
FROM spine sp
