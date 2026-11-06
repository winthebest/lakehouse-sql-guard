-- Derived from SarrangTech/fraud-detection-feature-store (MIT, see LICENSE in this directory),
-- file dbt/models/intermediate/int_volatility_features.sql at commit f7d8a259af37.
-- Mutated by operator L1-future-frame (L1):
-- the rolling window looks at the next N seconds instead of the previous N. This file is deliberately leaky.
-- Volatility features: how erratic this user's recent spending has been. Fraudulent
-- activity (account takeover, card testing) tends to break a user's normal spend
-- variance, which a stable rolling mean/count alone won't surface.

with txns as (
    select * from {{ ref('stg_transactions') }}
),

windowed as (
    select
        transaction_id,
        user_id,
        event_time,
        amount,

        stddev(amount) over (
            partition by user_id
            order by event_time_unix
            range between 1 following and {{ var('window_24h_seconds') }} following
        ) as amount_stddev_24h,

        stddev(amount) over (
            partition by user_id
            order by event_time_unix
            range between 1 following and {{ var('window_7d_seconds') }} following
        ) as amount_stddev_7d,

        avg(amount) over (
            partition by user_id
            order by event_time_unix
            range between 1 following and {{ var('window_7d_seconds') }} following
        ) as avg_amount_7d

    from txns
)

select
    transaction_id,
    user_id,
    event_time,
    amount_stddev_24h,
    amount_stddev_7d,
    -- Z-score of the current amount against the user's trailing 7d mean/stddev.
    -- Null-safe: with fewer than 2 prior transactions stddev is null/0, so we fall
    -- back to 0 (no volatility signal yet) rather than dividing by zero.
    case
        when coalesce(amount_stddev_7d, 0) = 0 then 0
        else (amount - avg_amount_7d) / amount_stddev_7d
    end as amount_zscore_7d
from windowed
