WITH a AS (
    SELECT act.*, p.group_1
    FROM act_train AS act
    JOIN people AS p ON p.people_id = act.people_id
)
SELECT
    t.activity_id,
    t.people_id,
    t.date,
    t.outcome,
    AVG(o.outcome) AS group_outcome_rate
FROM a AS t
LEFT JOIN a AS o
    ON o.group_1 = t.group_1
    AND o.date < t.date - INTERVAL 30 DAY
GROUP BY t.activity_id, t.people_id, t.date, t.outcome
