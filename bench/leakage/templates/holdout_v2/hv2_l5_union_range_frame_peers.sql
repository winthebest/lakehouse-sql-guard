-- leak: L5
-- param measure: amount | 1.0
-- Running total on a spine+purchases timeline at ingested_at, but with a RANGE frame: rows
-- tied with the spine row (purchases loaded at exactly t) are peers and are included.
WITH timeline AS (
    SELECT user_id, label_ts AS ts, TRUE AS is_spine, churned, CAST(NULL AS DOUBLE) AS amt
    FROM labels
    UNION ALL
    SELECT user_id, ingested_at AS ts, FALSE AS is_spine, CAST(NULL AS BIGINT) AS churned,
           CAST({measure} AS DOUBLE) AS amt
    FROM events
),
running AS (
    SELECT user_id, ts, is_spine, churned,
           SUM(amt) OVER (PARTITION BY user_id ORDER BY ts
                          RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS loaded_total
    FROM timeline
)
SELECT user_id, ts AS label_ts, churned,
       COALESCE(loaded_total, 0) AS loaded_purchase_total
FROM running
WHERE is_spine
