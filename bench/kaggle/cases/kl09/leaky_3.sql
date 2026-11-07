WITH promo_share AS (
    SELECT store_nbr, item_nbr, AVG(CAST(onpromotion AS DOUBLE)) AS promo_share
    FROM train
    GROUP BY store_nbr, item_nbr
)
SELECT
    t.id,
    t.date,
    t.store_nbr,
    t.item_nbr,
    t.unit_sales,
    s.promo_share
FROM train AS t
JOIN promo_share AS s ON s.store_nbr = t.store_nbr AND s.item_nbr = t.item_nbr
