-- leak: clean
WITH scored AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country,
           (SELECT COALESCE(SUM(e.amount), 0)
              FROM events e
             WHERE e.user_id = l.user_id
               AND e.ingested_at < l.label_ts
               AND e.event_ts >= l.label_ts - INTERVAL 30 DAY) AS spend_30d,
           (SELECT SUM(e.amount) / NULLIF(COUNT(DISTINCT e.user_id), 0)
              FROM events e
              JOIN entities x ON x.user_id = e.user_id
             WHERE x.country = en.country
               AND e.ingested_at < l.label_ts
               AND e.event_ts >= l.label_ts - INTERVAL 30 DAY) AS country_spend_per_buyer
    FROM labels l
    LEFT JOIN entities en ON en.user_id = l.user_id
)
SELECT user_id, label_ts, churned, spend_30d, country_spend_per_buyer,
       spend_30d / NULLIF(country_spend_per_buyer, 0) AS spend_vs_country
FROM scored
