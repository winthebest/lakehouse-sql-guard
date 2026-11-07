-- leak: L3
-- param agg: COUNT(*) | COUNT(DISTINCT h.item_id)
-- Lifetime practice of the user over every row of the table, with no upper time bound.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT {agg} FROM interactions h WHERE h.user_id = s.user_id) AS practice
FROM interactions s
