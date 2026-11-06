-- leak: L1
-- param n: 3 | 14
WITH timeline AS (
    SELECT user_id, ingested_at AS ts, amount,
           CAST(NULL AS TIMESTAMP) AS label_ts, CAST(NULL AS BIGINT) AS churned
    FROM events
    UNION ALL
    SELECT user_id, label_ts AS ts, CAST(NULL AS DOUBLE) AS amount, label_ts, churned
    FROM labels
),
scored AS (
    SELECT *,
           SUM(amount)   OVER w AS spend_loaded_next,
           COUNT(amount) OVER w AS n_loaded_next
    FROM timeline
    WINDOW w AS (PARTITION BY user_id ORDER BY ts
                 RANGE BETWEEN CURRENT ROW AND INTERVAL {n} DAY FOLLOWING)
)
SELECT user_id, label_ts, churned, spend_loaded_next, n_loaded_next
FROM scored
WHERE label_ts IS NOT NULL
