-- leak: L3
-- param agg: COUNT(*) | SUM(ev.amount)
-- param thr: 20 | 50
-- Leaky: the derived table aggregates big orders over all history and is joined to the prediction on user only.
SELECT lab.user_id, lab.label_ts, lab.churned, COALESCE(b.big, 0) AS big_orders
FROM labels AS lab
LEFT JOIN (
  SELECT ev.user_id, {agg} AS big FROM events AS ev WHERE ev.amount > {thr} GROUP BY ev.user_id
) AS b ON b.user_id = lab.user_id
