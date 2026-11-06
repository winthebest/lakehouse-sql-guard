-- leak: L3
SELECT l.user_id, l.label_ts, l.churned,
       SUM(e.amount) FILTER (WHERE dayofweek(e.event_ts) IN (0, 6))         AS weekend_spend,
       SUM(e.amount) FILTER (WHERE dayofweek(e.event_ts) BETWEEN 1 AND 5)   AS weekday_spend,
       COUNT(*)      FILTER (WHERE hour(e.event_ts) < 6)                    AS n_night_orders
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
GROUP BY ALL
