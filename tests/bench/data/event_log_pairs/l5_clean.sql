-- leak: clean
-- param agg: SUM | AVG
-- Earlier answers of the user, strictly before the prediction and already loaded.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT {agg}(h.correct) FROM interactions h
         WHERE h.user_id = s.user_id AND h.ts_ms < s.ts_ms AND h.loaded_ms < s.ts_ms) AS past_correct
FROM interactions s
