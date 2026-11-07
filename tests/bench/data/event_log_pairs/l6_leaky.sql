-- leak: L6
-- param agg: AVG | SUM
-- The share of correct answers of the item over every row, the row's own label included.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT {agg}(h.correct) FROM interactions h WHERE h.item_id = s.item_id) AS item_correct
FROM interactions s
