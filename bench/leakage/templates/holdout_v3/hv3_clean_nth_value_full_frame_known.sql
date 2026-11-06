-- leak: clean
-- A FOLLOWING frame is harmless here: the partition is one spine row's known events.
WITH known AS (
    SELECT l.user_id, l.label_ts, l.churned, e.event_id, e.event_ts, e.amount
    FROM labels l
    LEFT JOIN events e
           ON e.user_id = l.user_id
          AND e.ingested_at < l.label_ts
),
framed AS (
    SELECT *,
           first_value(amount)  OVER w AS last_known_amount,
           nth_value(amount, 2) OVER w AS second_last_known_amount,
           last_value(amount)   OVER w AS first_known_amount
    FROM known
    WINDOW w AS (PARTITION BY user_id, label_ts
                 ORDER BY event_ts DESC NULLS LAST, event_id DESC NULLS LAST
                 ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
)
SELECT DISTINCT user_id, label_ts, churned,
       last_known_amount, second_last_known_amount, first_known_amount
FROM framed
