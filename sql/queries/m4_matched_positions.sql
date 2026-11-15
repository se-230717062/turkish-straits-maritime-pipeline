-- M4: every AIS position with its nearest wave point and the wave reading of its own UTC hour.
-- Grain: one row per (vessel, observed_at); the join cannot fan out (rn = 1, wave_reading is unique per point and hour).
-- Flag filter: sog_flag IN (2 values), row_flag IN (2 values); wave_height_flag IN (3 values) on the wave side.
-- A position whose hour has no trusted wave reading keeps its row with NULL wave columns.
-- Parameters, in order: cos2, sog_flag x2, row_flag x2, wave_flag x3.
WITH d AS (
    SELECT p.vessel_key, p.observed_at, p.latitude, p.longitude, p.sog, p.nav_status, p.row_flag, m.point_key,
           (p.latitude - m.latitude) * (p.latitude - m.latitude)
         + ? * (p.longitude - m.longitude) * (p.longitude - m.longitude) AS dist2
    FROM ais_position_clean AS p CROSS JOIN marine_point AS m
    WHERE p.sog IS NOT NULL AND p.latitude IS NOT NULL AND p.longitude IS NOT NULL
      AND p.sog_flag IN (?, ?) AND p.row_flag IN (?, ?)
), nearest AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY vessel_key, observed_at ORDER BY dist2, point_key) AS rn FROM d
)
SELECT n.vessel_key, v.mmsi, n.observed_at, n.latitude, n.longitude, n.sog, n.nav_status, n.row_flag,
       n.point_key, n.dist2, w.wave_height, w.wave_period
FROM nearest AS n
JOIN vessel AS v ON v.vessel_key = n.vessel_key
LEFT JOIN wave_reading_clean AS w ON w.point_key = n.point_key
     AND w.observed_at = strftime('%Y-%m-%dT%H:00Z', n.observed_at) AND w.wave_height_flag IN (?, ?, ?)
WHERE n.rn = 1
ORDER BY n.observed_at, n.vessel_key;
