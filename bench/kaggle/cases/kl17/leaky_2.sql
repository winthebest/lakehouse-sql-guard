SELECT
    session_id,
    request_seq,
    request_time,
    leaves_site,
    COUNT(*) OVER (PARTITION BY session_id) AS page_count
FROM page_views
