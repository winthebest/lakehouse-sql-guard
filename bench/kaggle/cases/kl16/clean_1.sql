SELECT
    t.shot_id,
    t.game_date,
    t.shot_made_flag,
    AVG(h.shot_made_flag) AS zone_accuracy,
    COUNT(h.shot_id) AS zone_attempts
FROM data AS t
LEFT JOIN data AS h
    ON h.shot_zone_area = t.shot_zone_area
    AND h.game_date < t.game_date
GROUP BY t.shot_id, t.game_date, t.shot_made_flag
