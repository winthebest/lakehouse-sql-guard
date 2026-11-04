-- leak: L2
-- param ord: ev.event_ts DESC | ev.event_ts DESC, ev.event_id DESC | ev.ingested_at DESC, ev.event_id DESC
-- Leaky: DISTINCT ON keeps the newest event of all time for each prediction, including events after the prediction time.
SELECT DISTINCT ON (lab.user_id, lab.label_ts)
       lab.user_id, lab.label_ts, lab.churned, ev.amount AS last_amount, ev.event_ts AS last_event_ts
FROM labels AS lab
JOIN events AS ev ON ev.user_id = lab.user_id
ORDER BY lab.user_id, lab.label_ts, {ord}
