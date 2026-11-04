select
    labels.customer_id,
    labels.cutoff_ts,
    labels.churned,
    recent.orders_90d,
    recent.amount_90d,
    customers.first_name

from {{ source('raw', 'churn_labels') }} as labels

left join {{ ref('int_customer_recent_orders') }} as recent
    on recent.customer_id = labels.customer_id
    and recent.cutoff_ts = labels.cutoff_ts

left join {{ ref('stg_customers') }} as customers
    on customers.customer_id = labels.customer_id
