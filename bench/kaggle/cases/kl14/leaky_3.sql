WITH tx AS (
    SELECT
        *,
        CAST(card1 AS VARCHAR) || '_' || CAST(addr1 AS VARCHAR) || '_'
            || CAST(floor(TransactionDT / 86400) - D1 AS VARCHAR) AS uid
    FROM train_transaction
)
, uid_stats AS (
    SELECT uid, AVG(TransactionAmt) AS uid_amt_mean, STDDEV_SAMP(TransactionAmt) AS uid_amt_std
    FROM tx
    GROUP BY uid
)
SELECT
    t.TransactionID,
    t.card1,
    t.TransactionDT,
    t.isFraud,
    s.uid_amt_mean,
    s.uid_amt_std
FROM tx AS t
JOIN uid_stats AS s ON s.uid = t.uid
