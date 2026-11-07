SELECT
    timestamp,
    Asset_ID,
    Target,
    AVG(Target) OVER (
        PARTITION BY Asset_ID
        ORDER BY timestamp
        RANGE BETWEEN 1800 PRECEDING AND 960 PRECEDING
    ) AS target_mean_15
FROM train
