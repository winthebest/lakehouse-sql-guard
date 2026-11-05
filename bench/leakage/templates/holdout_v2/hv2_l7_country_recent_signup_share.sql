-- leak: L7
-- param days: 90 | 365 | 730
-- A country-level mix over the static entities table, but "recent" is relative to
-- current_date: the share changes with the day the query runs.
WITH country_mix AS (
    SELECT country,
           COUNT(*) FILTER (WHERE signup_ts >= current_date - INTERVAL {days} DAY) * 1.0
             / COUNT(*) AS recent_signup_share
    FROM entities
    GROUP BY country
)
SELECT l.user_id, l.label_ts, l.churned,
       cm.recent_signup_share
FROM labels l
JOIN entities en ON en.user_id = l.user_id
LEFT JOIN country_mix cm ON cm.country = en.country
