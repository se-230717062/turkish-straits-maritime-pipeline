-- (M3 revision of the M2 query) Question: when significant wave height exceeds 1.5 m / 2.5 m,
--           do vessels near our wave-grid points slow down, and does the share of waiting
--           vessels (Sog < 0.5 kn) grow?
-- Result grain: one row per (marine_point, wave-height class).
-- Each AIS position (grain: vessel-timestamp) is matched to exactly ONE wave point - the nearest
-- one (ROW_NUMBER over the position, rn = 1), within ~0.5 degrees - and to the wave reading of
-- its own UTC hour. The join cannot fan out: every position is counted at most once.
-- M3 changes: (1) reads ais_position_clean, so the sentinel filter "sog < 102.3" is gone from the
-- query (cleaning lives in sql/clean.sql), (2) longitude differences are scaled by cos(40 deg)^2
-- = 0.587, so the distance is no longer ~24% too long east-west.
WITH pos AS (
    SELECT p.vessel_key, p.observed_at, p.sog,
           strftime('%Y-%m-%dT%H:00Z', p.observed_at) AS obs_hour,
           m.point_key,
           (p.latitude - m.latitude) * (p.latitude - m.latitude)
         + 0.587 * (p.longitude - m.longitude) * (p.longitude - m.longitude) AS d2,
           ROW_NUMBER() OVER (
               PARTITION BY p.vessel_key, p.observed_at
               ORDER BY (p.latitude - m.latitude) * (p.latitude - m.latitude)
                      + 0.587 * (p.longitude - m.longitude) * (p.longitude - m.longitude)
           ) AS rn
    FROM ais_position_clean AS p
    CROSS JOIN marine_point AS m
    WHERE p.sog IS NOT NULL AND p.position_valid = 1
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
