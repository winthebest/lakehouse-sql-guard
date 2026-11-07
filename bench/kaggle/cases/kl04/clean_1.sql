SELECT
    fullVisitorId,
    visitId,
    visitStartTime,
    totals_transactionRevenue,
    visitStartTime - LAG(visitStartTime) OVER (
        PARTITION BY fullVisitorId ORDER BY visitStartTime
    ) AS secs_to_adjacent_visit
FROM train
