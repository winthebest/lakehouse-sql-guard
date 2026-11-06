-- leak: L1
-- param w: 7 | 30
-- date_diff(part, start, end) is end - start: with t as start this counts days AFTER t.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.event_id) AS n_purchases_window,
       SUM(e.amount)     AS spend_window
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND date_diff('day', l.label_ts, e.event_ts) BETWEEN 1 AND {w}
GROUP BY ALL
