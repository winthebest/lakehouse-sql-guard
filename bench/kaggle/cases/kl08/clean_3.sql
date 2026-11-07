WITH hist AS (
    SELECT Asset_ID, timestamp + 900 AS final_at, Target
    FROM train
)
SELECT
    t.timestamp,
    t.Asset_ID,
    t.Target,
    h.Target AS last_known_target
FROM train AS t
ASOF LEFT JOIN hist AS h
    ON h.Asset_ID = t.Asset_ID
    AND t.timestamp > h.final_at
