-- leak: L2
SELECT l.user_id, l.label_ts, l.churned,
       le.amount                                   AS latest_amount,
       date_diff('hour', le.event_ts, l.label_ts)  AS hours_since_latest_purchase
FROM labels l
LEFT JOIN (
    SELECT DISTINCT ON (user_id) user_id, amount, event_ts
    FROM staging.stg_events
    ORDER BY user_id, event_ts DESC, event_id DESC
) le ON le.user_id = l.user_id
