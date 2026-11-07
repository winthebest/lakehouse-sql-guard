SELECT
    t.Timestamp,
    t.TargetVariable,
    MAX(w.Variable74HIGH) / t.Variable74LAST_PRICE AS v74_high_ratio_60m
FROM training AS t
JOIN training AS w
    ON w.Timestamp >= t.Timestamp - INTERVAL 60 MINUTE
    AND w.Timestamp <= t.Timestamp
GROUP BY t.Timestamp, t.TargetVariable, t.Variable74LAST_PRICE
