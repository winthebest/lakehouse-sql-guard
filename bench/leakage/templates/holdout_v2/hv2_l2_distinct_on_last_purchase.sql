-- leak: L2
-- param join: LEFT JOIN | JOIN
-- DISTINCT ON keeps each user's newest purchase in the whole table, with no bound against t.
WITH last_purchase AS (
    SELECT DISTINCT ON (user_id) user_id, amount, event_ts
    FROM events
    ORDER BY user_id, event_ts DESC, event_id DESC
)
SELECT l.user_id, l.label_ts, l.churned,
       lp.amount AS last_purchase_amount,
       date_diff('day', lp.event_ts, l.label_ts) AS days_since_last_purchase
FROM labels l
{join} last_purchase lp ON lp.user_id = l.user_id
