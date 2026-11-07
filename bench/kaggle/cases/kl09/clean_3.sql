SELECT
    t.id,
    t.date,
    t.store_nbr,
    t.item_nbr,
    t.unit_sales,
    (
        SELECT AVG(CAST(h.onpromotion AS DOUBLE))
        FROM train AS h
        WHERE h.store_nbr = t.store_nbr
          AND h.item_nbr = t.item_nbr
          AND h.date < t.date
    ) AS promo_share
FROM train AS t
