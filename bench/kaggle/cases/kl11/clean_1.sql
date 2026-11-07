SELECT
    id,
    issue_d,
    loan_status,
    installment * CAST(trim(replace(term, 'months', '')) AS INTEGER) / loan_amnt
        AS repayment_ratio
FROM accepted_2007_to_2018q4
