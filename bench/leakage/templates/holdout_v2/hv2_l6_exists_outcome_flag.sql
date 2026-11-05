-- leak: L6
-- param min_refund: 0 | 5
-- An EXISTS against user_outcomes (rows exist only for churners) flags the outcome directly.
SELECT l.user_id, l.label_ts, l.churned,
       CASE WHEN EXISTS (
                SELECT 1
                FROM staging.stg_user_outcomes o
                WHERE o.user_id = l.user_id
                  AND COALESCE(o.refund_after_churn, 0) >= {min_refund})
            THEN 1 ELSE 0 END AS has_post_churn_record
FROM labels l
