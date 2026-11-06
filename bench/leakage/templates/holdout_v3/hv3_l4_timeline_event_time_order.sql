-- leak: L4
-- param agg: SUM | AVG | MAX
WITH timeline AS (
    SELECT user_id, event_ts AS ts, 1 AS kind, amount,
           CAST(NULL AS TIMESTAMP) AS label_ts, CAST(NULL AS BIGINT) AS churned
    FROM events
    UNION ALL
    SELECT user_id, label_ts AS ts, 0 AS kind, CAST(NULL AS DOUBLE) AS amount, label_ts, churned
    FROM labels
),
scored AS (
    SELECT *,
           {agg}(amount) OVER w AS spend_before,
           COUNT(amount) OVER w AS n_before
    FROM timeline
    WINDOW w AS (PARTITION BY user_id ORDER BY ts, kind
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
)
SELECT user_id, label_ts, churned, spend_before, n_before
FROM scored
WHERE kind = 0
