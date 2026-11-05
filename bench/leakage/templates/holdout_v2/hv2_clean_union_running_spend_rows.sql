-- leak: clean
-- param measure: amount | 1.0
-- One timeline of spine rows (at label_ts) and purchases (at ingested_at). Spine rows sort
-- before purchases at an equal timestamp (ord), and the ROWS frame ends at the current row,
-- so only purchases with ingested_at < t are summed.
WITH timeline AS (
    SELECT user_id, label_ts AS ts, 0 AS ord, churned, CAST(NULL AS DOUBLE) AS amt
    FROM labels
    UNION ALL
    SELECT user_id, ingested_at AS ts, 1 AS ord, CAST(NULL AS BIGINT) AS churned,
           CAST({measure} AS DOUBLE) AS amt
    FROM events
),
running AS (
    SELECT user_id, ts, ord, churned,
           SUM(amt) OVER (PARTITION BY user_id ORDER BY ts, ord
                          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS loaded_total
    FROM timeline
)
SELECT user_id, ts AS label_ts, churned,
       COALESCE(loaded_total, 0) AS loaded_purchase_total
FROM running
WHERE ord = 0
