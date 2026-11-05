-- leak: clean
-- param fn: MAX | MIN | AVG | MEDIAN
-- Scalar correlated subquery over the user's purchases loaded before t.
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT {fn}(e.amount)
        FROM events e
        WHERE e.user_id = l.user_id
          AND e.ingested_at < l.label_ts) AS known_ticket_stat
FROM labels l
