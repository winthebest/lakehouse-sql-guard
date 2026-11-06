-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT COUNT(*) FROM entities x
         WHERE x.country = en.country AND x.signup_ts < l.label_ts) AS country_users_signed_up,
       (SELECT COUNT(*) FROM entities x
         WHERE x.country = en.country
           AND x.signup_ts <  l.label_ts
           AND x.signup_ts >= l.label_ts - INTERVAL 30 DAY)        AS country_signups_30d
FROM labels l
LEFT JOIN entities en ON en.user_id = l.user_id
