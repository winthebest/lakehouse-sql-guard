SELECT
    row_id,
    user_id,
    timestamp,
    answered_correctly,
    AVG(answered_correctly) OVER (
        PARTITION BY user_id
        ORDER BY timestamp
        RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS user_correct_rate
FROM train
WHERE content_type_id = 0
