SELECT
    t.shot_id,
    t.game_date,
    t.shot_made_flag,
    AVG(h.shot_made_flag) AS recent_form_30d
FROM data AS t
LEFT JOIN data AS h
    ON h.game_date >= t.game_date - INTERVAL 30 DAY
    AND h.game_date < t.game_date
GROUP BY t.shot_id, t.game_date, t.shot_made_flag
