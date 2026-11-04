-- leak: L5
-- param agg: SUM | COUNT
-- Leaky: comparing ingestion and prediction by calendar day with <= admits the batch loaded at exactly midnight, which lands at the same instant as a midnight prediction.
SELECT lab.user_id, lab.label_ts, lab.churned, {agg}(ev.amount) AS amt_30d
FROM labels AS lab
LEFT JOIN events AS ev ON ev.user_id = lab.user_id
  AND CAST(ev.ingested_at AS DATE) <= CAST(lab.label_ts AS DATE)
  AND ev.event_ts >= lab.label_ts - INTERVAL '30 days'
GROUP BY lab.user_id, lab.label_ts, lab.churned
