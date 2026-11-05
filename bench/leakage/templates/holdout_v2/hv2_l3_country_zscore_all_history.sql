-- leak: L3
-- param days: 30 | 60
-- The user's recent average ticket is point-in-time, but it is standardised with the mean
-- and standard deviation of all purchases in the country over the whole table.
WITH spine AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country
    FROM labels l
    JOIN entities en ON en.user_id = l.user_id
),
recent AS (
    SELECT sp.user_id, sp.label_ts, AVG(e.amount) AS avg_ticket
    FROM spine sp
    JOIN events e
      ON e.user_id = sp.user_id
     AND e.ingested_at < sp.label_ts
     AND e.event_ts >= sp.label_ts - INTERVAL {days} DAY
    GROUP BY sp.user_id, sp.label_ts
),
country_norm AS (
    SELECT en.country, AVG(e.amount) AS mu, STDDEV_POP(e.amount) AS sd
    FROM events e
    JOIN entities en ON en.user_id = e.user_id
    GROUP BY en.country
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       (r.avg_ticket - cn.mu) / NULLIF(cn.sd, 0) AS ticket_zscore_in_country
FROM spine sp
LEFT JOIN recent r        ON r.user_id = sp.user_id AND r.label_ts = sp.label_ts
LEFT JOIN country_norm cn ON cn.country = sp.country
