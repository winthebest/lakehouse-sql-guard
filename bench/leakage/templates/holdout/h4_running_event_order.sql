-- leak: L4
-- param frame: ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW | ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
-- Leaky: the running total is ordered and cut by event time, so the newest row before the prediction may not have been ingested yet and late rows shift the total.
WITH run AS (
  SELECT user_id, event_ts, SUM(amount) OVER (PARTITION BY user_id ORDER BY event_ts, event_id {frame}) AS cum_spend
  FROM events
)
SELECT lab.user_id, lab.label_ts, lab.churned, r.cum_spend, r.event_ts AS last_event_ts
FROM labels AS lab
JOIN run AS r ON r.user_id = lab.user_id AND r.event_ts < lab.label_ts
QUALIFY ROW_NUMBER() OVER (PARTITION BY lab.user_id, lab.label_ts ORDER BY r.event_ts DESC) = 1
