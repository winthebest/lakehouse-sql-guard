SELECT
    Tail_Number,
    crs_dep_ts,
    ArrDel15,
    (DepTime // 100) * 60 + DepTime % 100 AS dep_minute_of_day
FROM flights
