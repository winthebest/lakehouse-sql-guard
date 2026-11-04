-- leak: clean
-- param fn: SUM | AVG
-- param frame: ROWS BETWEEN 4 PRECEDING AND CURRENT ROW | ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
-- Clean: the running window is evaluated only over events already ingested at the prediction time, and only the newest visible row is kept.
WITH vis AS (
  SELECT sp.user_id, sp.label_ts, sp.churned, ev.event_ts,
         {fn}(ev.amount) OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY ev.event_ts, ev.event_id {frame}) AS run_amt,
         ROW_NUMBER() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY ev.event_ts DESC, ev.event_id DESC) AS recency
  FROM labels AS sp
  JOIN events AS ev ON ev.user_id = sp.user_id
  WHERE ev.ingested_at < sp.label_ts
    AND ev.event_ts >= sp.label_ts - INTERVAL '90 days'
)
SELECT user_id, label_ts, churned, run_amt, event_ts AS last_event_ts
FROM vis
WHERE recency = 1
