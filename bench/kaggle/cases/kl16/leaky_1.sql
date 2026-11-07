WITH zone_acc AS (
    SELECT shot_zone_area, AVG(shot_made_flag) AS zone_accuracy, COUNT(*) AS zone_attempts
    FROM data
    GROUP BY shot_zone_area
)
SELECT
    t.shot_id,
    t.game_date,
    t.shot_made_flag,
    z.zone_accuracy,
    z.zone_attempts
FROM data AS t
JOIN zone_acc AS z ON z.shot_zone_area = t.shot_zone_area
