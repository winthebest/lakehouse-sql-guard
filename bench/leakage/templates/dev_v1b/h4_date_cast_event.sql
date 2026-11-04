-- leak: L4
-- param n: 7 | 30
-- Leaky: events are selected by event day only (strictly before the prediction day), so late-arriving events from those days are included before they landed.
SELECT lab.user_id, lab.label_ts, lab.churned,
       SUM(ev.amount) AS amt_{n}d, MAX(ev.event_ts) AS last_event_ts
FROM labels AS lab
LEFT JOIN events AS ev ON ev.user_id = lab.user_id
  AND CAST(ev.event_ts AS DATE) >= CAST(lab.label_ts AS DATE) - {n}
  AND CAST(ev.event_ts AS DATE) < CAST(lab.label_ts AS DATE)
GROUP BY lab.user_id, lab.label_ts, lab.churned
