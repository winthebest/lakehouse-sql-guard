SELECT
    Timestamp,
    TargetVariable,
    LEAD(Variable74LAST_PRICE) OVER (ORDER BY Timestamp) - Variable74LAST_PRICE AS v74_change
FROM training
