WITH flags AS (
    SELECT
        id,
        CASE WHEN recoveries > 0 OR debt_settlement_flag = 'Y' THEN 1 ELSE 0 END AS risk_flag,
        date_diff('month', issue_d, last_credit_pull_d) AS pull_age_months
    FROM accepted_2007_to_2018q4
)
SELECT l.id, l.issue_d, l.loan_status, f.risk_flag, f.pull_age_months
FROM accepted_2007_to_2018q4 AS l
JOIN flags AS f USING (id)
