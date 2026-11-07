SELECT
    id,
    date_account_created,
    country_destination,
    date_diff('day', date_account_created, date_first_booking) AS signup_lag_days
FROM train_users
