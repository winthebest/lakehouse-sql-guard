-- leak: L4
WITH known_plan AS (
    SELECT l.user_id, l.label_ts, l.churned, p.plan, p.valid_from, p.valid_to
    FROM labels l
    LEFT JOIN plan_history p
           ON p.user_id = l.user_id
          AND p.ingested_at < l.label_ts
    QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts
                               ORDER BY p.valid_from DESC NULLS LAST) = 1
)
SELECT user_id, label_ts, churned, plan,
       date_diff('hour', valid_from, LEAST(COALESCE(valid_to, label_ts), label_ts)) AS hours_on_plan,
       COALESCE(valid_to < label_ts, FALSE)                                       AS plan_already_ended
FROM known_plan
