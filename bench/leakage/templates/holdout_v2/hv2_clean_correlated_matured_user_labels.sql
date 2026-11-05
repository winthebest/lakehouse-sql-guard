-- leak: clean
-- param maturity: 14 | 15 | 21
-- The user's own earlier labels, but only those already final at t:
-- p.label_ts < t - maturity days, i.e. p.label_ts + maturity days < t, maturity >= 14.
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT COUNT(*) FROM labels p
        WHERE p.user_id = l.user_id
          AND p.label_ts < l.label_ts - INTERVAL {maturity} DAY) AS n_final_prior_labels,
       (SELECT COALESCE(SUM(p.churned), 0) FROM labels p
        WHERE p.user_id = l.user_id
          AND p.label_ts < l.label_ts - INTERVAL {maturity} DAY) AS n_final_prior_churns
FROM labels l
