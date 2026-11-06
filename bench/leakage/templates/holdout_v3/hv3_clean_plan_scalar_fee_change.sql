-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT p.monthly_fee FROM plan_history p
         WHERE p.user_id = l.user_id AND p.ingested_at < l.label_ts
         ORDER BY p.valid_from DESC LIMIT 1)
     - (SELECT p.monthly_fee FROM plan_history p
         WHERE p.user_id = l.user_id AND p.ingested_at < l.label_ts
         ORDER BY p.valid_from ASC LIMIT 1) AS fee_change_since_first_known
FROM labels l
