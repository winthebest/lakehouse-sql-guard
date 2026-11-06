-- leak: L1
SELECT l.user_id, l.label_ts, l.churned,
       date_diff('hour', l.label_ts, nx.event_ts) AS hours_to_next_purchase,
       nx.amount                                  AS next_purchase_amount
FROM marts.fct_training_spine l
LEFT JOIN LATERAL (
    SELECT e.event_ts, e.amount
    FROM staging.stg_events e
    WHERE e.user_id = l.user_id
      AND e.event_ts > l.label_ts
    ORDER BY e.event_ts, e.event_id
    LIMIT 1
) nx ON TRUE
