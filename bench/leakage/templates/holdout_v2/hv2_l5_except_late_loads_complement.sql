-- leak: L5
-- param days: 7 | 30 | 90
-- "Known purchases" built as a set difference: the purchases in the window EXCEPT those
-- loaded after t (ingested_at > t). The complement of > is <=, so purchases loaded at
-- exactly t (the midnight load at a midnight t) stay in, although they are not known at t.
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT COUNT(*)
        FROM (SELECT e.event_id FROM events e
              WHERE e.user_id = l.user_id
                AND e.event_ts >= l.label_ts - INTERVAL {days} DAY
              EXCEPT
              SELECT e.event_id FROM events e
              WHERE e.user_id = l.user_id
                AND e.ingested_at > l.label_ts) known) AS n_known_purchases_{days}d
FROM labels l
