SELECT
    t.id,
    t.date,
    t.store_nbr,
    t.item_nbr,
    t.unit_sales,
    CAST(p.onpromotion AS INTEGER) AS promo_7d_offset
FROM train AS t
LEFT JOIN train AS p
    ON p.store_nbr = t.store_nbr
    AND p.item_nbr = t.item_nbr
    AND p.date = t.date - 7
