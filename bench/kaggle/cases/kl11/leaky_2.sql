SELECT
    id,
    issue_d,
    loan_status,
    date_diff('month', issue_d, last_pymnt_d) AS history_months
FROM accepted_2007_to_2018q4
