-- leak: clean
-- param col: total_spend | n_orders
-- Forward fill over a timeline of spine rows and stats rows. At an equal timestamp the spine
-- row sorts first (src = 0), so a snapshot stamped exactly t is not carried into the row at t.
WITH tl AS (
    SELECT user_id, label_ts AS ts, 0 AS src, churned, CAST(NULL AS DOUBLE) AS v
    FROM labels
    UNION ALL
    SELECT user_id, updated_at AS ts, 1 AS src, CAST(NULL AS BIGINT) AS churned,
           CAST({col} AS DOUBLE) AS v
    FROM user_stats
),
filled AS (
    SELECT user_id, ts, src, churned,
           last_value(v IGNORE NULLS) OVER (PARTITION BY user_id ORDER BY ts, src
                                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS ffill
    FROM tl
)
SELECT user_id, ts AS label_ts, churned,
       ffill AS {col}_ffill
FROM filled
WHERE src = 0
