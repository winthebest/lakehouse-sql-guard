SELECT
    id,
    date_account_created,
    country_destination,
    CASE WHEN date_first_booking IS NULL THEN 1 ELSE 0 END AS missing_flag
FROM train_users
