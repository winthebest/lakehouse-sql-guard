-- leak: L6
SELECT l.user_id, l.label_ts, l.churned,
       en.country,
       CASE WHEN o.days_to_churn IS NULL THEN 'none'
            WHEN o.days_to_churn <= 3    THEN 'fast'
            ELSE 'slow' END AS churn_speed_bucket
FROM labels l
LEFT JOIN staging.stg_user_outcomes o ON o.user_id = l.user_id AND o.label_ts = l.label_ts
LEFT JOIN staging.stg_entities en     ON en.user_id = l.user_id
