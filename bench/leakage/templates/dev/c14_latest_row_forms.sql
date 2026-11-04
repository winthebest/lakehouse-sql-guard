-- leak: clean
-- param pick: = (SELECT MAX(x.updated_at) | = (SELECT MIN(x.updated_at) | IN (SELECT MAX(x.updated_at)
-- param col: s.total_spend | s.n_orders
SELECT l.user_id, l.label_ts, l.churned, {col} AS feat
FROM labels l
JOIN user_stats s ON s.user_id = l.user_id
WHERE s.updated_at {pick}
  FROM user_stats x WHERE x.user_id = l.user_id AND x.updated_at < l.label_ts)
