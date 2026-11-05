-- leak: L1
-- param ahead: 1 | 3 | 7
-- param behind: 7 | 30
-- RANGE frame on a spine+purchases timeline that extends ahead days FOLLOWING the spine row.
WITH timeline AS (
    SELECT user_id, label_ts AS ts, TRUE AS is_spine, churned, CAST(0 AS DOUBLE) AS amt
    FROM labels
    UNION ALL
    SELECT user_id, ingested_at AS ts, FALSE AS is_spine, CAST(NULL AS BIGINT) AS churned, amount AS amt
    FROM events
),
w AS (
    SELECT user_id, ts, is_spine, churned,
           SUM(amt) OVER (PARTITION BY user_id ORDER BY ts
                          RANGE BETWEEN INTERVAL {behind} DAY PRECEDING
                                    AND INTERVAL {ahead} DAY FOLLOWING) AS spend_around_t
    FROM timeline
)
SELECT user_id, ts AS label_ts, churned, spend_around_t
FROM w
WHERE is_spine
