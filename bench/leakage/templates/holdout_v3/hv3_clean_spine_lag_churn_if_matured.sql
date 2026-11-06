-- leak: clean
SELECT user_id, label_ts, churned,
       CASE WHEN LAG(label_ts) OVER w + INTERVAL 14 DAY < label_ts
            THEN LAG(churned) OVER w END AS prev_label_if_final
FROM labels
WINDOW w AS (PARTITION BY user_id ORDER BY label_ts)
