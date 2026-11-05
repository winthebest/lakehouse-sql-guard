-- leak: L4
-- param frame: UNBOUNDED PRECEDING | 20 PRECEDING
-- Same timeline shape as a correct running total, but purchases are placed at event_ts: the
-- frame takes purchases with event_ts < t whether or not they were loaded yet.
WITH timeline AS (
    SELECT user_id, label_ts AS ts, 0 AS ord, churned, CAST(NULL AS DOUBLE) AS amt
    FROM labels
    UNION ALL
    SELECT user_id, event_ts AS ts, 1 AS ord, CAST(NULL AS BIGINT) AS churned, amount AS amt
    FROM events
),
running AS (
    SELECT user_id, ts, ord, churned,
           SUM(amt) OVER (PARTITION BY user_id ORDER BY ts, ord
                          ROWS BETWEEN {frame} AND CURRENT ROW) AS spend_running
    FROM timeline
)
SELECT user_id, ts AS label_ts, churned,
       COALESCE(spend_running, 0) AS spend_running
FROM running
WHERE ord = 0
