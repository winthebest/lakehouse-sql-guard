SELECT
    t.fullVisitorId,
    t.visitId,
    t.visitStartTime,
    t.totals_transactionRevenue,
    (
        SELECT COUNT(*)
        FROM train AS o
        WHERE o.fullVisitorId = t.fullVisitorId
          AND o.visitStartTime < t.visitStartTime
    ) + 1 AS user_visits
FROM train AS t
