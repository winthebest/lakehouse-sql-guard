SELECT
    Tail_Number,
    crs_dep_ts,
    ArrDel15,
    DepDelay AS delay_context_min
FROM flights
