-- leak: L3
-- param w: 14 | 45
-- The list is built over all history: its mere existence for a user (len 0 rather than NULL)
-- depends on events loaded at or after t, even though every element used is filtered.
WITH user_event_lists AS (
    SELECT user_id,
           list(struct_pack(ts := event_ts, loaded := ingested_at, amt := amount)) AS evts
    FROM events
    GROUP BY user_id
)
SELECT l.user_id, l.label_ts, l.churned,
       list_sum(list_transform(
           list_filter(ue.evts, lambda x: x.loaded < l.label_ts AND x.ts >= l.label_ts - INTERVAL {w} DAY),
           lambda x: x.amt))                                               AS spend_window_known,
       len(list_filter(ue.evts, lambda x: x.loaded < l.label_ts))           AS n_orders_known
FROM labels l
LEFT JOIN user_event_lists ue ON ue.user_id = l.user_id
