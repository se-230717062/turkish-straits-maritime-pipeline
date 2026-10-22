-- Question: when significant wave height exceeds 1.5 m / 2.5 m, do vessels near our wave-grid
--           points slow down, and does the share of vessels that are waiting (Sog < 0.5 kn) grow?
-- Result grain: one row per (marine_point, wave-height class).
-- Each AIS position (grain: vessel-timestamp) is matched to exactly ONE wave point - the nearest
-- one (ROW_NUMBER over the position, rn = 1), within ~0.5 degrees - and to the wave reading of
-- its own UTC hour. The join therefore cannot fan out: every position is counted at most once,
-- and AVG / SUM below aggregate positions at their own grain.
-- Non-numeric values were converted to NULL at load time, so AVG skips them.
-- Query-time filter (not cleaning): sog < 102.3 excludes the AIS "not available" code.
-- "Waiting" = Sog < 0.5 kn. Anchorage polygons are not modelled yet (M3/M4), so this is
-- "slow or stopped near the wave point", not "inside an anchorage".
WITH pos AS (
    SELECT p.vessel_key, p.observed_at, p.sog,
           strftime('%Y-%m-%dT%H:00Z', p.observed_at) AS obs_hour,
           m.point_key,
           (p.latitude - m.latitude) * (p.latitude - m.latitude)
         + (p.longitude - m.longitude) * (p.longitude - m.longitude) AS d2,
           ROW_NUMBER() OVER (
               PARTITION BY p.vessel_key, p.observed_at
               ORDER BY (p.latitude - m.latitude) * (p.latitude - m.latitude)
                      + (p.longitude - m.longitude) * (p.longitude - m.longitude)
           ) AS rn
    FROM ais_position AS p
    CROSS JOIN marine_point AS m
    WHERE p.sog IS NOT NULL AND p.sog < 102.3
      AND p.latitude IS NOT NULL AND p.longitude IS NOT NULL
)
SELECT m.latitude AS point_lat, m.longitude AS point_lon,
       CASE WHEN w.wave_height <= 1.5 THEN '1 calm (<= 1.5 m)'
            WHEN w.wave_height <= 2.5 THEN '2 rough (1.5-2.5 m)'
            ELSE '3 severe (> 2.5 m)' END           AS wave_class,
       COUNT(*)                                     AS n_positions,
       ROUND(AVG(pos.sog), 2)                       AS avg_sog_knots,
       SUM(pos.sog < 0.5)                           AS n_waiting,
       ROUND(100.0 * SUM(pos.sog < 0.5) / COUNT(*), 1) AS pct_waiting
FROM pos
JOIN wave_reading AS w ON w.point_key = pos.point_key AND w.observed_at = pos.obs_hour
JOIN marine_point AS m ON m.point_key = pos.point_key
WHERE pos.rn = 1 AND pos.d2 <= 0.25 AND w.wave_height IS NOT NULL
GROUP BY m.point_key, wave_class
ORDER BY point_lat, point_lon, wave_class;