select
    labels.customer_id,
    labels.cutoff_ts,
    labels.churned,
    features.lifetime_orders,
    features.lifetime_amount,
    customers.first_name

from {{ source('raw', 'churn_labels') }} as labels

left join {{ ref('int_customer_features') }} as features
    on features.customer_id = labels.customer_id

left join {{ ref('stg_customers') }} as customers
    on customers.customer_id = labels.customer_id
