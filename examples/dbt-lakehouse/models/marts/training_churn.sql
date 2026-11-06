select
    labels.customer_id,
    labels.cutoff_ts,
    labels.churned,
    recent.orders_7d,
    recent.amount_7d,
    customers.first_name

from {{ source('raw', 'churn_labels') }} as labels

left join {{ ref('int_customer_orders_7d') }} as recent
    on recent.customer_id = labels.customer_id
    and recent.cutoff_ts = labels.cutoff_ts

left join {{ ref('stg_customers') }} as customers
    on customers.customer_id = labels.customer_id
