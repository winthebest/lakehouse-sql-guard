SELECT
    id,
    date_account_created,
    country_destination,
    CASE WHEN age IS NULL THEN 1 ELSE 0 END AS missing_flag
FROM train_users
