SELECT
    t.timestamp,
    t.Asset_ID,
    t.Target,
    p.Target AS last_known_target
FROM train AS t
ASOF LEFT JOIN train AS p
    ON p.Asset_ID = t.Asset_ID
    AND t.timestamp > p.timestamp
