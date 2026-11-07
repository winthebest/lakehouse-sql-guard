SELECT
    t.timestamp,
    t.Asset_ID,
    t.Target,
    p.Target AS target_lag
FROM train AS t
LEFT JOIN train AS p
    ON p.Asset_ID = t.Asset_ID
    AND p.timestamp = t.timestamp - 960
