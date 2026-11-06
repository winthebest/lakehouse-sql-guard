-- leak: L7
SELECT l.user_id, l.label_ts, l.churned,
       CASE WHEN en.signup_ts >= current_localtimestamp() - INTERVAL 90 DAY THEN 'new'
            ELSE 'tenured' END AS account_segment
FROM labels l
LEFT JOIN entities en ON en.user_id = l.user_id
