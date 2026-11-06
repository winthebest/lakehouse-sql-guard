-- leak: L2
SELECT l.user_id, l.label_ts, l.churned,
       ll.amount                                    AS last_loaded_amount,
       date_diff('hour', ll.ingested_at, l.label_ts) AS hours_since_last_load
FROM labels l
LEFT JOIN LATERAL (
    SELECT e.amount, e.ingested_at
    FROM events e
    WHERE e.user_id = l.user_id
    ORDER BY e.ingested_at DESC, e.event_id DESC
    LIMIT 1
) ll ON TRUE
