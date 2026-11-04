-- Question: when significant wave height exceeds 1.5 m / 2.5 m, do vessels INSIDE an anchorage
--           slow down / does the share of waiting vessels (Sog < 0.5 kn) grow?
-- Result grain: one row per (anchorage, wave-height class).
-- Fan-out control: ais_in_anchorage has one row per (position, anchorage) and polygons do not
-- overlap, so a position appears once. anchorage_wave_point gives ONE wave point per anchorage and
-- wave_reading is unique per (point, hour), so the hour join adds no rows.
-- Cleaning comes from ais_position_clean: sentinel Sog (102.3) is already NULL and is excluded here.
-- "Waiting" = Sog < 0.5 kn INSIDE an anchorage polygon. A snapshot, not a duration (see README).
SELECT a.name AS anchorage,
       CASE WHEN w.wave_height <= 1.5 THEN '1 calm (<= 1.5 m)'
            WHEN w.wave_height <= 2.5 THEN '2 rough (1.5-2.5 m)'
            ELSE '3 severe (> 2.5 m)' END               AS wave_class,
       COUNT(*)                                          AS n_positions,
       COUNT(DISTINCT p.vessel_key)                      AS n_vessels,
       ROUND(AVG(p.sog), 2)                              AS avg_sog_knots,
       SUM(p.sog < 0.5)                                  AS n_waiting,
       ROUND(100.0 * SUM(p.sog < 0.5) / COUNT(*), 1)     AS pct_waiting
FROM ais_in_anchorage AS ia
JOIN ais_position_clean AS p ON p.vessel_key = ia.vessel_key AND p.observed_at = ia.observed_at
JOIN anchorage AS a ON a.anchorage_key = ia.anchorage_key
JOIN anchorage_wave_point AS awp ON awp.anchorage_key = ia.anchorage_key
JOIN wave_reading AS w ON w.point_key = awp.point_key
                      AND w.observed_at = strftime('%Y-%m-%dT%H:00Z', p.observed_at)
WHERE p.sog IS NOT NULL AND w.wave_height IS NOT NULL
GROUP BY a.anchorage_key, wave_class
ORDER BY a.anchorage_key, wave_class;
