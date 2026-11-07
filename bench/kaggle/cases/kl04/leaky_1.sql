SELECT
    fullVisitorId,
    visitId,
    visitStartTime,
    totals_transactionRevenue,
    LEAD(visitStartTime) OVER (
        PARTITION BY fullVisitorId ORDER BY visitStartTime
    ) - visitStartTime AS secs_to_adjacent_visit
FROM train
