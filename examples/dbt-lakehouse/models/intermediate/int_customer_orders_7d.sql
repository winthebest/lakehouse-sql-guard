{#- Orders and spend of each customer in the 7 days before each prediction date. -#}
select
    labels.customer_id,
    labels.cutoff_ts,
    count(orders.id) as orders_7d,
    coalesce(sum(orders.amount), 0) as amount_7d

from {{ source('raw', 'churn_labels') }} as labels

left join {{ source('raw', 'orders') }} as orders
    on orders.user_id = labels.customer_id
    and orders.ordered_at < labels.cutoff_ts
    and orders.ordered_at >= labels.cutoff_ts - interval 7 day

group by labels.customer_id, labels.cutoff_ts
