-- M4 key number, CLEANED: same question, read from the M3 cleaned layer with an explicit flag filter.
-- Flag filter: sog_flag IN (2 values), row_flag IN (2 values), wave_height_flag IN (3 values), all from the
-- notebook parameters; a NULL cleaned value never counts.
-- Parameters, in the order of the ? marks: cos2, sog_flag x2, row_flag x2, wait_kn, wait_kn, radius_squared, wave_flag x3.
WITH d AS (
    SELECT p.vessel_key, p.observed_at, p.sog, m.point_key,
           (p.latitude - m.latitude) * (p.latitude - m.latitude)
         + ? * (p.longitude - m.longitude) * (p.longitude - m.longitude) AS dist2
    FROM ais_position_clean AS p CROSS JOIN marine_point AS m
    WHERE p.sog IS NOT NULL AND p.latitude IS NOT NULL AND p.longitude IS NOT NULL
      AND p.sog_flag IN (?, ?) AND p.row_flag IN (?, ?)
), nearest AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY vessel_key, observed_at ORDER BY dist2, point_key) AS rn FROM d
)
SELECT COUNT(*) AS n_positions, SUM(n.sog < ?) AS n_waiting,
       ROUND(100.0 * SUM(n.sog < ?) / COUNT(*), 1) AS pct_waiting, ROUND(AVG(n.sog), 2) AS mean_sog
FROM nearest AS n
JOIN wave_reading_clean AS w ON w.point_key = n.point_key AND w.observed_at = strftime('%Y-%m-%dT%H:00Z', n.observed_at)
WHERE n.rn = 1 AND n.dist2 <= ? AND w.wave_height IS NOT NULL AND w.wave_height_flag IN (?, ?, ?);
