WITH ip_app_counts AS (
    SELECT ip, app, COUNT(*) AS ip_app_count
    FROM train
    GROUP BY ip, app
)
SELECT
    t.click_id,
    t.ip,
    t.click_time,
    t.is_attributed,
    c.ip_app_count
FROM train AS t
JOIN ip_app_counts AS c ON c.ip = t.ip AND c.app = t.app
