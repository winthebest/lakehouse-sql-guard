WITH user_stats AS (
    SELECT
        user_id,
        AVG(answered_correctly) AS user_correct_rate,
        COUNT(*) AS user_questions
    FROM train
    WHERE content_type_id = 0
    GROUP BY user_id
)
SELECT
    t.row_id,
    t.user_id,
    t.timestamp,
    t.answered_correctly,
    s.user_correct_rate,
    s.user_questions
FROM train AS t
JOIN user_stats AS s ON s.user_id = t.user_id
WHERE t.content_type_id = 0
