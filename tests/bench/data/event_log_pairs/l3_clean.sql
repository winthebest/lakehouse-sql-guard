-- leak: clean
-- param agg: COUNT(*) | COUNT(DISTINCT h.item_id)
-- Lifetime practice of the user up to the prediction, from rows loaded by then.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT {agg} FROM interactions h
         WHERE h.user_id = s.user_id AND h.ts_ms < s.ts_ms AND h.loaded_ms < s.ts_ms) AS practice
FROM interactions s
