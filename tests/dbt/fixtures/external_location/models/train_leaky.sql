{{ config(materialized='view', meta={'leakage': {'role': 'training_set'}}) }}

select l.user_id, l.label_ts, l.churned, sum(e.amount) as lifetime_amount
from {{ source('lake', 'labels') }} as l
left join {{ source('lake', 'events') }} as e on e.user_id = l.user_id
group by 1, 2, 3
