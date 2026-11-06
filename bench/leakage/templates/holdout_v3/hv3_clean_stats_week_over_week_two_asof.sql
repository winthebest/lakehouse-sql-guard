-- leak: clean
-- param d: 7 | 14
WITH spine AS (
    SELECT user_id, label_ts, churned, label_ts - INTERVAL {d} DAY AS back_ts
    FROM labels
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       cur.total_spend - bak.total_spend AS spend_gain_{d}d,
       cur.n_orders    - bak.n_orders    AS orders_gain_{d}d
FROM spine sp
ASOF LEFT JOIN user_stats cur ON cur.user_id = sp.user_id AND sp.label_ts > cur.updated_at
ASOF LEFT JOIN user_stats bak ON bak.user_id = sp.user_id AND sp.back_ts  > bak.updated_at
