SELECT
    click_id,
    ip,
    click_time,
    is_attributed,
    date_diff('second', MAX(click_time) OVER (
        PARTITION BY ip, app, device, os
        ORDER BY click_time
        RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 SECOND PRECEDING
    ), click_time) AS click_gap_secs
FROM train
