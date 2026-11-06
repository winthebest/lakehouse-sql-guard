-- leak: clean
SELECT l.user_id, l.label_ts, l.churned, p.plan, p.monthly_fee, p.valid_from AS plan_since
FROM labels l
LEFT JOIN LATERAL (
    SELECT ph.plan, ph.monthly_fee, ph.valid_from
    FROM plan_history ph
    WHERE ph.user_id = l.user_id
      AND ph.ingested_at < l.label_ts
    ORDER BY ph.valid_from DESC, ph.ingested_at DESC
    LIMIT 1
) p ON TRUE
