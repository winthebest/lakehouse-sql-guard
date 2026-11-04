-- leak: clean
-- param mature: 14 | 21 | 30
-- Clean: the group churn rate uses only labels old enough that their outcome window had already closed at the prediction time (the label horizon is 14 days).
SELECT sp.user_id, sp.label_ts, sp.churned,
       (SELECT AVG(h.churned)
        FROM labels AS h
        JOIN entities AS he ON he.user_id = h.user_id
        WHERE he.country = u.country
          AND h.label_ts <= sp.label_ts - INTERVAL '{mature} days') AS country_churn_rate
FROM labels AS sp
JOIN entities AS u ON u.user_id = sp.user_id
