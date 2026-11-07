-- leak: L5
-- param agg: SUM | AVG
-- The bound is inclusive: the answer stamped at the prediction time itself is counted.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT {agg}(h.correct) FROM interactions h
         WHERE h.user_id = s.user_id AND h.ts_ms <= s.ts_ms) AS past_correct
FROM interactions s
