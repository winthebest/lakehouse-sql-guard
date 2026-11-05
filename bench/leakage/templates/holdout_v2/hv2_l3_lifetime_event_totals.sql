-- leak: L3
-- param agg: SUM(amount) | COUNT(*) | AVG(amount) | MAX(amount)
-- A per-user aggregate over every purchase in the table, joined with no bound against t.
SELECT l.user_id, l.label_ts, l.churned,
       t.v AS lifetime_purchase_stat
FROM labels l
LEFT JOIN (
    SELECT user_id, {agg} AS v
    FROM events
    GROUP BY user_id
) t ON t.user_id = l.user_id
