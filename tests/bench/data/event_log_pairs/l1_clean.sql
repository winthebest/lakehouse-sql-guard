-- leak: clean
-- param window: 1 | 6
-- Practice in the hour(s) before the prediction, counting only rows that were loaded by then.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT COUNT(*) FROM interactions h
         WHERE h.user_id = s.user_id
           AND h.loaded_ms < s.ts_ms
           AND h.ts_ms < s.ts_ms
           AND h.ts_ms >= s.ts_ms - {window} * 3600000) AS recent_attempts
FROM interactions s
