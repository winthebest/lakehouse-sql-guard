-- leak: L6
SELECT l.user_id, l.label_ts, l.churned,
       EXISTS (SELECT 1 FROM user_outcomes o
                WHERE o.user_id = l.user_id
                  AND o.label_ts < l.label_ts) AS churned_before
FROM labels l
