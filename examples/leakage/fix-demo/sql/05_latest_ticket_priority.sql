-- L2: "the latest ticket" is chosen over all history. The fix picks the latest one before each
-- cutoff (an ASOF join in DuckDB and Snowflake, a ROW_NUMBER elsewhere).
SELECT
    l.customer_id,
    l.cutoff_ts,
    l.churned,
    t.priority AS last_ticket_priority
FROM churn_labels l
LEFT JOIN (
    SELECT * FROM support_tickets
    QUALIFY row_number() OVER (PARTITION BY customer_id ORDER BY opened_at DESC) = 1
) t ON t.customer_id = l.customer_id
