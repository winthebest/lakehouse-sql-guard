-- leak: L3
-- param join: JOIN | LEFT JOIN
-- Leaky: the last-seen timestamp and event count come from a window over all of the user's events with no time restriction.
SELECT DISTINCT lab.user_id, lab.label_ts, lab.churned,
       MAX(ev.event_ts) OVER (PARTITION BY lab.user_id, lab.label_ts) AS last_event_ts,
       COUNT(*) OVER (PARTITION BY lab.user_id, lab.label_ts) AS n_events_total
FROM labels AS lab
{join} events AS ev ON ev.user_id = lab.user_id
