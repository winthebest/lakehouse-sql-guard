-- leak: clean
-- param thr: 40 | 80
-- Clean: the correlated IN subquery only lists users with a large order that had already been ingested at the prediction time.
SELECT lab.user_id, lab.label_ts, lab.churned,
       CAST(lab.user_id IN (
         SELECT ev.user_id FROM events AS ev
         WHERE ev.ingested_at < lab.label_ts AND ev.amount >= {thr}
       ) AS INTEGER) AS has_big_order
FROM labels AS lab
