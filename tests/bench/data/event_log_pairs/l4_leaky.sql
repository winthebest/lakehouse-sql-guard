-- leak: L4
-- param lag: 0 | 1
-- A bound on the event time only: rows that happened earlier but were loaded later are counted.
SELECT s.user_id, s.ts_ms, s.correct,
       (SELECT COUNT(*) FROM interactions h
         WHERE h.user_id = s.user_id AND h.ts_ms < s.ts_ms - {lag} * 3600000) AS practice
FROM interactions s
