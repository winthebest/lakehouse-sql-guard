-- leak: L1
-- param window: 1 | 6
-- The same count, but over the hour(s) after the prediction.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT COUNT(*) FROM interactions h
         WHERE h.user_id = s.user_id
           AND h.ts_ms > s.ts_ms
           AND h.ts_ms <= s.ts_ms + {window} * 3600000) AS upcoming_attempts
FROM interactions s
