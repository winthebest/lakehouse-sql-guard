WITH per_user AS (
    SELECT fullVisitorId, COUNT(*) AS user_visits
    FROM train
    GROUP BY fullVisitorId
)
SELECT
    t.fullVisitorId,
    t.visitId,
    t.visitStartTime,
    t.totals_transactionRevenue,
    u.user_visits
FROM train AS t
JOIN per_user AS u ON u.fullVisitorId = t.fullVisitorId
