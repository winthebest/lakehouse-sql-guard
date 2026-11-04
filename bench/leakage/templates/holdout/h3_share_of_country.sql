-- leak: L3
-- param expr: pu.u_spend / pc.c_spend | pu.u_spend - pc.c_spend / 60.0
-- Leaky: per-user and per-country totals are aggregated over all events ever ingested, not just those before the prediction.
WITH per_user AS (
  SELECT user_id, SUM(amount) AS u_spend FROM events GROUP BY user_id
),
per_country AS (
  SELECT en.country, SUM(pu.u_spend) AS c_spend
  FROM per_user AS pu
  JOIN entities AS en USING (user_id)
  GROUP BY en.country
)
SELECT lab.user_id, lab.label_ts, lab.churned, {expr} AS share
FROM labels AS lab
JOIN entities AS en ON en.user_id = lab.user_id
JOIN per_user AS pu ON pu.user_id = lab.user_id
JOIN per_country AS pc ON pc.country = en.country
