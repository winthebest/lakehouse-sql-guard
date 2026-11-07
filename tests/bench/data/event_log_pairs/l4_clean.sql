-- leak: clean
-- param lag: 3 | 4
-- Only history older than the longest load delay: whatever it covers has been loaded.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT COUNT(*) FROM interactions h
         WHERE h.user_id = s.user_id AND h.ts_ms < s.ts_ms - {lag} * 86400000) AS old_practice
FROM interactions s
