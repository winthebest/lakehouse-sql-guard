WITH tx AS (
    SELECT
        *,
        CAST(card1 AS VARCHAR) || '_' || CAST(addr1 AS VARCHAR) || '_'
            || CAST(floor(TransactionDT / 86400) - D1 AS VARCHAR) AS uid
    FROM train_transaction
)
SELECT
    t.TransactionID,
    t.card1,
    t.TransactionDT,
    t.isFraud,
    AVG(h.TransactionAmt) AS uid_amt_mean,
    STDDEV_SAMP(h.TransactionAmt) AS uid_amt_std
FROM tx AS t
LEFT JOIN tx AS h ON h.uid = t.uid AND h.TransactionDT < t.TransactionDT
GROUP BY t.TransactionID, t.card1, t.TransactionDT, t.isFraud
