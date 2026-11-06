-- leak: clean
-- param agg: SUM | AVG | MAX
-- Events placed on the timeline at their load time; at a tie the spine row (kind 0) sorts
-- before the load (kind 1), and the frame stops one row before the spine row.
WITH timeline AS (
    SELECT user_id, ingested_at AS ts, 1 AS kind, amount,
           CAST(NULL AS TIMESTAMP) AS label_ts, CAST(NULL AS BIGINT) AS churned
    FROM staging.stg_events
    UNION ALL
    SELECT user_id, label_ts AS ts, 0 AS kind, CAST(NULL AS DOUBLE) AS amount, label_ts, churned
    FROM labels
),
scored AS (
    SELECT *,
           {agg}(amount) OVER w AS spend_loaded_before,
           COUNT(amount) OVER w AS n_loaded_before
    FROM timeline
    WINDOW w AS (PARTITION BY user_id ORDER BY ts, kind
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
)
SELECT user_id, label_ts, churned, spend_loaded_before, n_loaded_before
FROM scored
WHERE kind = 0
