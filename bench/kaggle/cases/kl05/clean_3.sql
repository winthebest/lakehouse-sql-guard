SELECT
    click_id,
    ip,
    click_time,
    is_attributed,
    COUNT(*) OVER (
        PARTITION BY ip, app
        ORDER BY click_time
        RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 SECOND PRECEDING
    ) AS ip_app_count
FROM train
