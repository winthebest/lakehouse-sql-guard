SELECT
    t.row_id,
    t.user_id,
    t.timestamp,
    t.answered_correctly,
    SUM(h.answered_correctly) AS prior_correct,
    COUNT(h.row_id) AS prior_answered
FROM train AS t
LEFT JOIN train AS h
    ON h.user_id = t.user_id
    AND h.content_type_id = 0
    AND h.timestamp < t.timestamp
WHERE t.content_type_id = 0
GROUP BY t.row_id, t.user_id, t.timestamp, t.answered_correctly
