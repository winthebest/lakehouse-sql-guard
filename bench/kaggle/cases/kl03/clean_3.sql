SELECT
    Timestamp,
    TargetVariable,
    AVG(Variable142LAST_PRICE) OVER (
        ORDER BY Timestamp ROWS BETWEEN 12 PRECEDING AND CURRENT ROW
    ) - Variable142LAST_PRICE AS v142_ma_gap
FROM training
