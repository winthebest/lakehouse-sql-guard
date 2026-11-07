-- leak: clean
-- param agg: AVG | SUM
-- The item's earlier answers, loaded by the prediction time (the label is final when stamped).
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT {agg}(h.correct) FROM interactions h
         WHERE h.item_id = s.item_id AND h.ts_ms < s.ts_ms AND h.loaded_ms < s.ts_ms) AS item_correct
FROM interactions s
