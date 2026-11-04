-- leak: clean
-- param bound: ev.ingested_at < date_trunc('day', lab.label_ts) | ev.ingested_at < CAST(lab.label_ts AS DATE)
-- param agg: SUM | COUNT
-- Clean: availability is cut at the start of the prediction day, which is never later than the prediction time.
SELECT lab.user_id, lab.label_ts, lab.churned, {agg}(ev.amount) AS amt_30d
FROM labels AS lab
LEFT JOIN events AS ev ON ev.user_id = lab.user_id
  AND {bound}
  AND ev.event_ts >= lab.label_ts - INTERVAL '30 days'
GROUP BY lab.user_id, lab.label_ts, lab.churned
