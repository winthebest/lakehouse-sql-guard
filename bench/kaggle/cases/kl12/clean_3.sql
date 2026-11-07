SELECT
    t.Tail_Number,
    t.crs_dep_ts,
    t.ArrDel15,
    COUNT(p.crs_dep_ts) AS rotation_signal
FROM flights AS t
LEFT JOIN flights AS p
    ON p.Tail_Number = t.Tail_Number
    AND p.FlightDate = t.FlightDate
    AND p.crs_dep_ts < t.crs_dep_ts
GROUP BY t.Tail_Number, t.crs_dep_ts, t.ArrDel15
