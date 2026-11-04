-- leak: L3
-- param fn: SUM(amount) | COUNT(*) | AVG(amount)
-- Leaky: a window over each user's entire event history is joined to every prediction, so events after the prediction time are counted.
WITH hist AS (
  SELECT DISTINCT user_id, {fn} OVER (PARTITION BY user_id) AS hist_value FROM events
)
SELECT lab.user_id, lab.label_ts, lab.churned, h.hist_value
FROM labels AS lab
LEFT JOIN hist AS h USING (user_id)
