{#-
    Lifetime order statistics per customer. The aggregate is not tied to a prediction date, so
    for a prediction made in March it also counts the orders of April, May and June. This is the
    leak the guard finds in `training_churn_leaky`, which joins this model; it is reported
    against this file.
-#}
select
    customer_id,
    count(*) as lifetime_orders,
    sum(amount) as lifetime_amount,
    max(ordered_at) as last_order_at

from {{ ref('stg_orders') }}

group by customer_id
