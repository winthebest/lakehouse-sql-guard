SELECT
    click_id,
    ip,
    click_time,
    is_attributed,
    date_diff('second', click_time, LEAD(click_time) OVER (
        PARTITION BY ip, app, device, os ORDER BY click_time
    )) AS click_gap_secs
FROM train
