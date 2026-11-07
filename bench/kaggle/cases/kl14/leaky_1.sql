WITH tx AS (
    SELECT
        *,
        CAST(card1 AS VARCHAR) || '_' || CAST(addr1 AS VARCHAR) || '_'
            || CAST(floor(TransactionDT / 86400) - D1 AS VARCHAR) AS uid
    FROM train_transaction
)
SELECT
    TransactionID,
    card1,
    TransactionDT,
    isFraud,
    AVG(TransactionAmt) OVER (PARTITION BY uid) AS uid_amt_mean,
    COUNT(*) OVER (PARTITION BY uid) AS uid_tx_count
FROM tx
