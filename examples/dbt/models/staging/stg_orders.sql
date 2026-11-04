select
    id as order_id,
    user_id as customer_id,
    ordered_at,
    loaded_at,
    status,
    amount

from {{ source('raw', 'orders') }}
