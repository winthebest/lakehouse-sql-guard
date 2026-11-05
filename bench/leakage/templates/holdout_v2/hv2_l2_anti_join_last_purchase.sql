-- leak: L2
-- param thr: 20 | 50
-- "Latest row" by anti-join: a purchase with no later purchase of the same user, over the
-- whole table and with no bound against t.
WITH last_purchase AS (
    SELECT e.user_id, e.amount
    FROM events e
    WHERE NOT EXISTS (
        SELECT 1 FROM events e2
        WHERE e2.user_id = e.user_id
          AND (e2.event_ts > e.event_ts
               OR (e2.event_ts = e.event_ts AND e2.event_id > e.event_id)))
)
SELECT l.user_id, l.label_ts, l.churned,
       CASE WHEN lp.amount >= {thr} THEN 'high'
            WHEN lp.amount IS NOT NULL THEN 'low' END AS last_purchase_size
FROM labels l
LEFT JOIN last_purchase lp ON lp.user_id = l.user_id
