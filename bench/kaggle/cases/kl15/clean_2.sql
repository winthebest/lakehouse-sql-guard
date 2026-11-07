WITH a AS (
    SELECT act.*, p.group_1
    FROM act_train AS act
    JOIN people AS p ON p.people_id = act.people_id
)
, group_day AS (
    SELECT group_1, date + INTERVAL 30 DAY AS final_date, AVG(outcome) AS day_rate
    FROM a
    GROUP BY group_1, date
)
SELECT
    t.activity_id,
    t.people_id,
    t.date,
    t.outcome,
    g.day_rate AS group_neighbour_rate
FROM a AS t
ASOF LEFT JOIN group_day AS g
    ON g.group_1 = t.group_1
    AND t.date > g.final_date
