-- leak: L3
WITH ranked AS (
    SELECT user_id,
           percent_rank() OVER (ORDER BY SUM(amount)) AS spend_pctile,
           percent_rank() OVER (ORDER BY COUNT(*))    AS orders_pctile
    FROM analytics.staging.stg_events
    GROUP BY user_id
)
SELECT l.user_id, l.label_ts, l.churned, r.spend_pctile, r.orders_pctile
FROM labels l
LEFT JOIN ranked r ON r.user_id = l.user_id
