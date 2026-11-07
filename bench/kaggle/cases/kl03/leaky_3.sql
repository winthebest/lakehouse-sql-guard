SELECT
    Timestamp,
    TargetVariable,
    AVG(Variable142LAST_PRICE) OVER (
        ORDER BY Timestamp ROWS BETWEEN CURRENT ROW AND 12 FOLLOWING
    ) - Variable142LAST_PRICE AS v142_ma_gap
FROM training
