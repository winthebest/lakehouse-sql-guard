-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       EXISTS (SELECT 1 FROM plan_history p
                WHERE p.user_id = l.user_id AND p.ingested_at < l.label_ts AND p.plan = 'pro')  AS ever_pro_known,
       EXISTS (SELECT 1 FROM plan_history p
                WHERE p.user_id = l.user_id AND p.ingested_at < l.label_ts AND p.plan = 'free') AS ever_free_known
FROM labels l
