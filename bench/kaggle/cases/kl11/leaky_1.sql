SELECT
    id,
    issue_d,
    loan_status,
    total_pymnt / loan_amnt AS repayment_ratio
FROM accepted_2007_to_2018q4
