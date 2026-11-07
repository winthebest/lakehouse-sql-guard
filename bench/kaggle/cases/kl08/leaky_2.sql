SELECT
    timestamp,
    Asset_ID,
    Target,
    AVG(Target) OVER (
        PARTITION BY Asset_ID
        ORDER BY timestamp
        RANGE BETWEEN 900 PRECEDING AND 60 PRECEDING
    ) AS target_mean_15
FROM train
