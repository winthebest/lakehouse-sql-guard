-- leak: L1
-- param upper: sp.label_ts + INTERVAL '7 days' | sp.label_ts + INTERVAL '14 days' | date_add(sp.label_ts, INTERVAL 7 DAY) | date_add(sp.label_ts, INTERVAL 14 DAY)
-- Leaky: the lateral subquery sums events that happen after the prediction time, which cannot have been observed yet.
SELECT sp.user_id, sp.label_ts, sp.churned, f.future_spend, f.next_event_ts
FROM labels AS sp
LEFT JOIN LATERAL (
  SELECT SUM(ev.amount) AS future_spend, MIN(ev.event_ts) AS next_event_ts
  FROM events AS ev
  WHERE ev.user_id = sp.user_id
    AND ev.event_ts >= sp.label_ts
    AND ev.event_ts < {upper}
) AS f ON TRUE
