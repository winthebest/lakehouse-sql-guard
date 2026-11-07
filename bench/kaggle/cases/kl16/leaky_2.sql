SELECT
    shot_id,
    game_date,
    shot_made_flag,
    AVG(shot_made_flag) OVER (
        PARTITION BY combined_shot_type
        ORDER BY game_date
        RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS type_accuracy
FROM data
