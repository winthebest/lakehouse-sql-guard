SELECT
    Timestamp,
    TargetVariable,
    Variable74LAST_PRICE - LAG(Variable74LAST_PRICE) OVER (ORDER BY Timestamp) AS v74_change
FROM training
