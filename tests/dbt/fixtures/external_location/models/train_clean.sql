{{ config(materialized='view', meta={'leakage': {'role': 'training_set'}}) }}

select l.user_id, l.label_ts, l.churned, count(e.amount) as n_events
from {{ source('lake', 'labels') }} as l
left join {{ source('lake', 'events') }} as e
  on e.user_id = l.user_id
 and e.event_ts < l.label_ts
 and e.ingested_at < l.label_ts
group by 1, 2, 3
