-- leak: L6
-- param tiers: 3 | 5
-- An ASOF join from the spine to user_outcomes (label_ts >= outcome label_ts). For a churned
-- row it matches that row's own outcome, so refund tier and fast-churn flag are outcome facts.
WITH outcome_tiers AS (
    SELECT user_id, label_ts AS outcome_label_ts, days_to_churn,
           ntile({tiers}) OVER (ORDER BY refund_after_churn) AS refund_tier
    FROM user_outcomes
)
SELECT l.user_id, l.label_ts, l.churned,
       o.refund_tier,
       o.days_to_churn <= 7 AS fast_churner
FROM labels l
ASOF LEFT JOIN outcome_tiers o
  ON o.user_id = l.user_id
 AND l.label_ts >= o.outcome_label_ts
