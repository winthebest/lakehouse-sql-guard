SELECT
    id,
    date_account_created,
    country_destination,
    date_diff(
        'day',
        CAST(strptime(CAST(timestamp_first_active AS VARCHAR), '%Y%m%d%H%M%S') AS DATE),
        date_account_created
    ) AS signup_lag_days
FROM train_users
