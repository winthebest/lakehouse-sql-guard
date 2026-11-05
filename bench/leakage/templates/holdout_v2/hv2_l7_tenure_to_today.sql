-- leak: L7
-- param clock: current_date | now() | today()
-- param unit: day | month
-- Tenure is measured to the day the query runs, not to t.
SELECT l.user_id, l.label_ts, l.churned,
       en.country,
       date_diff('{unit}', en.signup_ts, CAST({clock} AS TIMESTAMP)) AS tenure_{unit}s
FROM labels l
JOIN entities en ON en.user_id = l.user_id
