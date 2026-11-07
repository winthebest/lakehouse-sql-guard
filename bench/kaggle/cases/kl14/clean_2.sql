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
    MAX(h.isFraud) AS uid_fraud_seen
FROM tx AS t
LEFT JOIN tx AS h
    ON h.uid = t.uid
    AND h.TransactionDT < t.TransactionDT - 120 * 86400
GROUP BY t.TransactionID, t.card1, t.TransactionDT, t.isFraud
