-- leak: clean
-- param days: 30 | 90
-- Numerator and normaliser are both bounded by ingested_at < t: the user's loaded spend over
-- the country's loaded spend in the same window.
WITH spine AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country
    FROM labels l
    JOIN entities en ON en.user_id = l.user_id
),
user_spend AS (
    SELECT sp.user_id, sp.label_ts, SUM(e.amount) AS spend
    FROM spine sp
    JOIN events e
      ON e.user_id = sp.user_id
     AND e.ingested_at < sp.label_ts
     AND e.event_ts >= sp.label_ts - INTERVAL {days} DAY
    GROUP BY sp.user_id, sp.label_ts
),
country_spend AS (
    SELECT sp.user_id, sp.label_ts, SUM(e.amount) AS spend
    FROM spine sp
    JOIN entities peer ON peer.country = sp.country
    JOIN events e
      ON e.user_id = peer.user_id
     AND e.ingested_at < sp.label_ts
     AND e.event_ts >= sp.label_ts - INTERVAL {days} DAY
    GROUP BY sp.user_id, sp.label_ts
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       COALESCE(us.spend, 0) / NULLIF(cs.spend, 0) AS share_of_country_spend_{days}d
FROM spine sp
LEFT JOIN user_spend us    ON us.user_id = sp.user_id AND us.label_ts = sp.label_ts
LEFT JOIN country_spend cs ON cs.user_id = sp.user_id AND cs.label_ts = sp.label_ts
