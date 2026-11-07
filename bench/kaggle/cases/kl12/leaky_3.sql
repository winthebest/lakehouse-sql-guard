SELECT
    Tail_Number,
    crs_dep_ts,
    ArrDel15,
    COALESCE(CarrierDelay, 0) + COALESCE(WeatherDelay, 0) + COALESCE(NASDelay, 0)
        + COALESCE(SecurityDelay, 0) + COALESCE(LateAircraftDelay, 0) AS rotation_signal
FROM flights
