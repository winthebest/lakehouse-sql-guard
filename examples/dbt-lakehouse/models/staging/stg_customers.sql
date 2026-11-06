select
    id as customer_id,
    first_name

from {{ source('raw', 'customers') }}
