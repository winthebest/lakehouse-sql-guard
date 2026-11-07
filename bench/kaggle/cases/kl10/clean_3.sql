WITH dated AS (
    SELECT *, date_account_created AS ref_date FROM train_users
)
SELECT
    id,
    date_account_created,
    country_destination,
    month(ref_date) AS ref_month,
    dayofweek(ref_date) AS ref_weekday
FROM dated
