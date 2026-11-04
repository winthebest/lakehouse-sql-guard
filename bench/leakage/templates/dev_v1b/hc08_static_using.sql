-- leak: clean
-- param tenure: date_diff('day', ent.signup_ts, lab.label_ts) | CAST(lab.label_ts AS DATE) - CAST(ent.signup_ts AS DATE)
-- param bucket: ent.country | CASE WHEN ent.country IN ('VN', 'JP') THEN 'APAC' ELSE 'OTHER' END
-- Clean: only static entity attributes and a signup date that precedes every prediction.
SELECT lab.user_id, lab.label_ts, lab.churned, {bucket} AS region, {tenure} AS tenure, ent.signup_ts
FROM labels AS lab
JOIN entities AS ent USING (user_id)
