WITH flags AS (
    SELECT
        id,
        CASE WHEN dti > 30 OR installment * 12 > 0.2 * annual_inc THEN 1 ELSE 0 END AS risk_flag,
        date_diff('month', earliest_cr_line, issue_d) AS pull_age_months
    FROM accepted_2007_to_2018q4
)
SELECT l.id, l.issue_d, l.loan_status, f.risk_flag, f.pull_age_months
FROM accepted_2007_to_2018q4 AS l
JOIN flags AS f USING (id)
