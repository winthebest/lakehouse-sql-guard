{#-
    Order statistics as of each prediction date: only orders placed in the 90 days before the
    cutoff and already loaded by then. The bounds sit in the join, so customers without orders
    keep their row.
-#}
select
    labels.customer_id,
    labels.cutoff_ts,
    count(orders.order_id) as orders_90d,
    coalesce(sum(orders.amount), 0) as amount_90d

from {{ source('raw', 'churn_labels') }} as labels

left join {{ ref('stg_orders') }} as orders
    on orders.customer_id = labels.customer_id
    and orders.ordered_at < labels.cutoff_ts
    and orders.loaded_at < labels.cutoff_ts
    and orders.ordered_at >= labels.cutoff_ts - interval 90 day

group by labels.customer_id, labels.cutoff_ts
