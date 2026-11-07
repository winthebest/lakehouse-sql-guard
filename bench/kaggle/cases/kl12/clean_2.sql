SELECT
    Tail_Number,
    crs_dep_ts,
    ArrDel15,
    (CRSDepTime // 100) * 60 + CRSDepTime % 100 AS dep_minute_of_day
FROM flights
