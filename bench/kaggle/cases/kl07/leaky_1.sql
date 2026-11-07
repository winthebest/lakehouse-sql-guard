SELECT
    row_id,
    user_id,
    timestamp,
    answered_correctly,
    AVG(answered_correctly) OVER (
        PARTITION BY user_id
        ORDER BY timestamp, row_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS user_cum_correct_rate
FROM train
WHERE content_type_id = 0
