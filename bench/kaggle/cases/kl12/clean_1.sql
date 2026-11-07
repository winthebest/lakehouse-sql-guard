WITH legs AS (
    SELECT
        *,
        (CRSDepTime // 100) * 60 + CRSDepTime % 100 AS dep_min,
        LAG((CRSArrTime // 100) * 60 + CRSArrTime % 100) OVER (
            PARTITION BY Tail_Number, FlightDate ORDER BY crs_dep_ts
        ) AS prev_arr_min
    FROM flights
)
SELECT
    Tail_Number,
    crs_dep_ts,
    ArrDel15,
    dep_min - prev_arr_min AS delay_context_min
FROM legs
