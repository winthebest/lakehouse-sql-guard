SELECT
    session_id,
    request_seq,
    request_time,
    leaves_site,
    ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY request_time) AS page_count
FROM page_views
