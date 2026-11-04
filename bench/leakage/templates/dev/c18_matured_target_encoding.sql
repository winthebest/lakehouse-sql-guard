-- leak: clean
-- param mature: 14 | 21 | 30
-- param agg: AVG | SUM
SELECT sp.user_id, sp.label_ts, sp.churned,
       (SELECT {agg}(h.churned)
        FROM labels AS h
        JOIN entities AS he ON he.user_id = h.user_id
        WHERE he.country = u.country
          AND h.label_ts <= sp.label_ts - INTERVAL {mature} DAY) AS country_churn
FROM labels AS sp
JOIN entities AS u ON u.user_id = sp.user_id
