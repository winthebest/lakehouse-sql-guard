SELECT
    timestamp,
    Asset_ID,
    Target,
    LAG(Target) OVER (PARTITION BY Asset_ID ORDER BY timestamp) AS target_lag
FROM train
