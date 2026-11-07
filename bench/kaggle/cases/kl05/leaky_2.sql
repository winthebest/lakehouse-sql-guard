SELECT
    t.click_id,
    t.ip,
    t.click_time,
    t.is_attributed,
    COUNT(o.click_id) AS clicks_1h
FROM train AS t
LEFT JOIN train AS o
    ON o.ip = t.ip AND o.app = t.app AND o.device = t.device AND o.os = t.os
    AND o.click_time > t.click_time
    AND o.click_time <= t.click_time + INTERVAL 1 HOUR
GROUP BY t.click_id, t.ip, t.click_time, t.is_attributed
