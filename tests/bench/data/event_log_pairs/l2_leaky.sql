-- leak: L2
-- param order: DESC
-- The newest difficulty estimate of the item, with no bound on when it was computed.
SELECT s.user_id, s.ts_ms, s.correct, st.difficulty
FROM interactions s
LEFT JOIN item_stats st ON st.item_id = s.item_id
QUALIFY row_number() OVER (PARTITION BY s.user_id, s.ts_ms ORDER BY st.updated_ms {order}) = 1
