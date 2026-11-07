SELECT
    t.session_id,
    t.request_seq,
    t.request_time,
    t.leaves_site,
    COUNT(p.request_seq) + 1 AS pages_in_session
FROM page_views AS t
LEFT JOIN page_views AS p
    ON p.session_id = t.session_id
    AND p.request_time < t.request_time
GROUP BY t.session_id, t.request_seq, t.request_time, t.leaves_site
