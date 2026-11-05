-- leak: L3
-- param q: 0.5 | 0.75 | 0.9
-- IN (... GROUP BY ... HAVING) over the whole events table, with a data-driven threshold
-- that is itself an all-history aggregate.
SELECT l.user_id, l.label_ts, l.churned,
       CASE WHEN l.user_id IN (
                SELECT user_id
                FROM events
                GROUP BY user_id
                HAVING SUM(amount) >= (
                    SELECT quantile_cont(s, {q})
                    FROM (SELECT SUM(amount) AS s FROM events GROUP BY user_id) per_user))
            THEN 1 ELSE 0 END AS is_high_value_user
FROM labels l
