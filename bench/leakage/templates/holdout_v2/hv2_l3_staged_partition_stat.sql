-- leak: L3
-- param fn: AVG | STDDEV_POP | MAX
-- The staging layer attaches a window aggregate over the user's whole partition (all history)
-- to every purchase. The join keeps only purchases loaded before t, but the attached value
-- was computed over all of them.
WITH stg_events AS (
    SELECT user_id, event_ts, ingested_at, amount,
           {fn}(amount) OVER (PARTITION BY user_id) AS user_amount_stat
    FROM events
)
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.amount)         AS n_loaded_purchases,
       MAX(e.user_amount_stat) AS user_amount_stat
FROM labels l
LEFT JOIN stg_events e
  ON e.user_id = l.user_id
 AND e.ingested_at < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
