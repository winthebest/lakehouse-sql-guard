SELECT
    t.fullVisitorId,
    t.visitId,
    t.visitStartTime,
    t.totals_transactionRevenue,
    COUNT(o.visitId) AS visits_30d
FROM train AS t
LEFT JOIN train AS o
    ON o.fullVisitorId = t.fullVisitorId
    AND o.visitStartTime > t.visitStartTime
    AND o.visitStartTime <= t.visitStartTime + 30 * 86400
GROUP BY t.fullVisitorId, t.visitId, t.visitStartTime, t.totals_transactionRevenue
