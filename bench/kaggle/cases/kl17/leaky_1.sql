SELECT
    session_id,
    request_seq,
    request_time,
    leaves_site,
    session_length AS pages_in_session
FROM page_views
