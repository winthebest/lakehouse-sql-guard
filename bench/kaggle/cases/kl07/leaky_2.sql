WITH q AS (
    SELECT * FROM train WHERE content_type_id = 0
)
SELECT
    row_id,
    user_id,
    timestamp,
    answered_correctly,
    SUM(answered_correctly) OVER w AS prior_correct,
    COUNT(*) OVER w AS prior_answered
FROM q
WINDOW w AS (
    PARTITION BY user_id
    ORDER BY timestamp, task_container_id, row_id
    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
)
