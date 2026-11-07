SELECT
    t.row_id,
    t.user_id,
    t.timestamp,
    t.answered_correctly,
    (
        SELECT AVG(h.answered_correctly)
        FROM train AS h
        WHERE h.user_id = t.user_id
          AND h.content_type_id = 0
          AND h.timestamp < t.timestamp
    ) AS user_correct_rate
FROM train AS t
WHERE t.content_type_id = 0
