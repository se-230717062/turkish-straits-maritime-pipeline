-- M4 key number, RAW: share of AIS positions that are waiting (sog < ?) among positions whose nearest
-- wave point lies within the radius, in the hour of a wave reading. Reads the M2 tables as loaded.
-- Flag filter: none (raw values; the AIS "not available" code 102.3 would be averaged as a speed).
-- Parameters: cos2, wait_kn, wait_kn, radius_squared.
WITH d AS (
    SELECT p.vessel_key, p.observed_at, p.sog, m.point_key,
           (p.latitude - m.latitude) * (p.latitude - m.latitude)
         + ? * (p.longitude - m.longitude) * (p.longitude - m.longitude) AS dist2
    FROM ais_position AS p CROSS JOIN marine_point AS m
    WHERE p.sog IS NOT NULL AND p.latitude IS NOT NULL AND p.longitude IS NOT NULL
), nearest AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY vessel_key, observed_at ORDER BY dist2, point_key) AS rn FROM d
)
SELECT COUNT(*) AS n_positions, SUM(n.sog < ?) AS n_waiting,
       ROUND(100.0 * SUM(n.sog < ?) / COUNT(*), 1) AS pct_waiting, ROUND(AVG(n.sog), 2) AS mean_sog
FROM nearest AS n
JOIN wave_reading AS w ON w.point_key = n.point_key AND w.observed_at = strftime('%Y-%m-%dT%H:00Z', n.observed_at)
WHERE n.rn = 1 AND n.dist2 <= ? AND w.wave_height IS NOT NULL;
