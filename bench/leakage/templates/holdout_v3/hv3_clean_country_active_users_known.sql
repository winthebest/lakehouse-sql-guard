-- leak: clean
-- param d: 7 | 14
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT COUNT(DISTINCT e.user_id)
          FROM events e
          JOIN entities x ON x.user_id = e.user_id
         WHERE x.country = en.country
           AND e.ingested_at < l.label_ts
           AND e.event_ts >= l.label_ts - INTERVAL {d} DAY) AS active_buyers_in_country
FROM labels l
LEFT JOIN entities en ON en.user_id = l.user_id
