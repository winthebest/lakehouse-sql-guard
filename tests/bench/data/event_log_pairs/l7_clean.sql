-- leak: clean
-- param days: 30 | 60
-- The wall clock only chooses which rows to train on; no feature depends on it.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT COUNT(*) FROM interactions h
         WHERE h.user_id = s.user_id AND h.ts_ms < s.ts_ms AND h.loaded_ms < s.ts_ms) AS practice
FROM interactions s
WHERE to_timestamp(s.ts_ms / 1000) < now() - INTERVAL {days} DAY
