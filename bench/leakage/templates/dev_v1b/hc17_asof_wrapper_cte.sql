-- leak: clean
-- param expr: total_spend / NULLIF(n_orders, 0) | ln(1 + total_spend) | date_diff('hour', updated_at, label_ts) | n_orders * 2
-- Clean: the strict ASOF join picks the snapshot published before the prediction; the outer query only transforms it.
WITH snap AS (
  SELECT sp.user_id, sp.label_ts, sp.churned, st.total_spend, st.n_orders, st.updated_at
  FROM labels AS sp
  ASOF LEFT JOIN user_stats AS st ON sp.user_id = st.user_id AND sp.label_ts > st.updated_at
)
SELECT user_id, label_ts, churned, {expr} AS feat
FROM snap
