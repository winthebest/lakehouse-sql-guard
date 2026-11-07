SELECT
    id,
    date,
    store_nbr,
    item_nbr,
    unit_sales,
    SUM(CASE WHEN onpromotion THEN 1 ELSE 0 END) OVER (
        PARTITION BY store_nbr, item_nbr
        ORDER BY date
        RANGE BETWEEN INTERVAL 14 DAY PRECEDING AND INTERVAL 1 DAY PRECEDING
    ) AS promo_days_14
FROM train
