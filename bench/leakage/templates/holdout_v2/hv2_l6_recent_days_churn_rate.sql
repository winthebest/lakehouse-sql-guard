-- leak: L6
-- param lag_days: 1 | 3 | 7
-- "Churn rate of the last few days of predictions": spine rows from calendar days 1 to
-- lag_days before the day of t. Those labels are at most 7 days old, so not final at t.
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT AVG(pb.churned)
        FROM labels pb
        WHERE CAST(pb.label_ts AS DATE) BETWEEN CAST(l.label_ts AS DATE) - {lag_days}
                                            AND CAST(l.label_ts AS DATE) - 1) AS recent_days_churn_rate
FROM labels l
