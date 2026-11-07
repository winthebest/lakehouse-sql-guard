-- leak: L7
-- A feature that depends on the day the query is run.
SELECT s.user_id, s.ts_ms, s.correct,
       date_diff('day', to_timestamp(s.ts_ms / 1000), now()) AS age_in_days
FROM interactions s
