-- leak: L6
-- param pred: EXISTS (SELECT 1 FROM user_outcomes AS o WHERE o.user_id = lab.user_id AND o.label_ts = lab.label_ts) | lab.user_id IN (SELECT o.user_id FROM user_outcomes AS o) | EXISTS (SELECT 1 FROM user_outcomes AS o WHERE o.user_id = lab.user_id AND o.days_to_churn <= 7)
-- Leaky: the flag is derived from the outcomes table, whose rows only exist for users who went on to churn.
SELECT lab.user_id, lab.label_ts, lab.churned,
       CASE WHEN {pred} THEN 1 ELSE 0 END AS followup_flag
FROM labels AS lab
