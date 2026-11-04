-- leak: L3
-- param set: UNION ALL | UNION
-- param agg: SUM | COUNT
SELECT sp.user_id, sp.label_ts, sp.churned, {agg}(e.amount) AS lifetime
FROM (
  SELECT user_id, label_ts, churned FROM labels WHERE user_id % 2 = 0
  {set}
  SELECT user_id, label_ts, churned FROM labels WHERE user_id % 2 = 1
) sp
LEFT JOIN events e ON e.user_id = sp.user_id
GROUP BY sp.user_id, sp.label_ts, sp.churned
