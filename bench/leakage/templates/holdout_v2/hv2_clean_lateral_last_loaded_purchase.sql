-- leak: clean
-- param order_col: ingested_at | event_ts
-- LATERAL "latest row" restricted to purchases loaded before t; ties broken by event_id.
SELECT l.user_id, l.label_ts, l.churned,
       lp.amount AS last_known_amount,
       date_diff('hour', lp.event_ts, l.label_ts) AS hours_since_last_known_purchase
FROM labels l
LEFT JOIN LATERAL (
    SELECT e.amount, e.event_ts
    FROM events e
    WHERE e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
    ORDER BY e.{order_col} DESC, e.event_id DESC
    LIMIT 1
) lp ON TRUE
