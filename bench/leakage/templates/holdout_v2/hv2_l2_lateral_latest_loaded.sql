-- leak: L2
-- param min_amount: 0 | 10
-- LATERAL ORDER BY ingested_at DESC LIMIT 1 without any bound against t: the most recently
-- loaded purchase in today's table.
SELECT l.user_id, l.label_ts, l.churned,
       x.amount AS last_loaded_amount,
       date_diff('hour', x.event_ts, x.ingested_at) AS last_load_delay_hours
FROM labels l
LEFT JOIN LATERAL (
    SELECT e.amount, e.event_ts, e.ingested_at
    FROM events e
    WHERE e.user_id = l.user_id
      AND e.amount > {min_amount}
    ORDER BY e.ingested_at DESC, e.event_id DESC
    LIMIT 1
) x ON TRUE
