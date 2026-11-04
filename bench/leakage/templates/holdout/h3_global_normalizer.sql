-- leak: L3
-- param stat: AVG | MAX | STDDEV_POP
-- Leaky: the as-of spend is clean but it is divided by a statistic computed over every stats row ever written, including later ones.
SELECT sp.user_id, sp.label_ts, sp.churned,
       st.total_spend / (SELECT {stat}(total_spend) FROM user_stats) AS spend_rel
FROM labels AS sp
ASOF JOIN user_stats AS st ON sp.user_id = st.user_id AND sp.label_ts > st.updated_at
