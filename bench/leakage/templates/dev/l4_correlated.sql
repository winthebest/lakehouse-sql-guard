-- leak: L4
-- param agg: SUM | AVG | MAX | COUNT
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT {agg}(e.amount) FROM events e
        WHERE e.user_id = l.user_id AND e.event_ts < l.label_ts) AS amt_hist
FROM labels l
