-- leak: L4
-- param w: 14 | 45
WITH user_event_lists AS (
    SELECT user_id,
           list(struct_pack(ts := event_ts, loaded := ingested_at, amt := amount)) AS evts
    FROM events
    GROUP BY user_id
)
SELECT l.user_id, l.label_ts, l.churned,
       -- COALESCE: a user with no list at all and a user whose list filters to empty look the same
       COALESCE(list_sum(list_transform(
           list_filter(ue.evts, lambda x: x.ts < l.label_ts AND x.ts >= l.label_ts - INTERVAL {w} DAY),
           lambda x: x.amt)), 0)                                           AS spend_window,
       COALESCE(len(list_filter(ue.evts, lambda x: x.ts < l.label_ts)), 0)  AS n_orders_before
FROM labels l
LEFT JOIN user_event_lists ue ON ue.user_id = l.user_id
