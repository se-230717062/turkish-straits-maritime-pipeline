-- M3 cleaning layer. The raw table ais_position is NEVER modified (M2 keeps data as received).
-- All cleaning lives in this view, so every rule is visible, countable and reversible.
-- AIS "not available" codes (ITU-R M.1371): SOG 102.3, COG 360, heading 511,
-- rate of turn -128, navigational status 15, latitude 91, longitude 181.
DROP VIEW IF EXISTS ais_position_clean;
CREATE VIEW ais_position_clean AS
SELECT vessel_key, observed_at,
       CASE WHEN position_ok THEN latitude  END AS latitude,
       CASE WHEN position_ok THEN longitude END AS longitude,
       position_ok AS position_valid,
       CASE WHEN sog >= 0 AND sog < 102.3 THEN sog END AS sog,
       CASE WHEN cog >= 0 AND cog < 360 THEN cog END AS cog,
       CASE WHEN true_heading BETWEEN 0 AND 359 THEN true_heading END AS true_heading,
       CASE WHEN nav_status BETWEEN 0 AND 14 THEN nav_status END AS nav_status,
       CASE WHEN rate_of_turn BETWEEN -127 AND 127 THEN rate_of_turn END AS rate_of_turn,
       retrieved_at, source_file
FROM (
    SELECT *,
           COALESCE(latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180
                    AND NOT (latitude = 0 AND longitude = 0), 0) AS position_ok
    FROM ais_position
);

-- One row per cleaning rule: how many raw rows it changed (a non-NULL raw value became NULL).
DROP VIEW IF EXISTS cleaning_log;
CREATE VIEW cleaning_log AS
SELECT 'sog: 102.3 / out of range -> NULL' AS rule, COUNT(*) AS n_rows
  FROM ais_position WHERE sog IS NOT NULL AND NOT (sog >= 0 AND sog < 102.3)
UNION ALL
SELECT 'cog: 360 / out of range -> NULL', COUNT(*)
  FROM ais_position WHERE cog IS NOT NULL AND NOT (cog >= 0 AND cog < 360)
UNION ALL
SELECT 'true_heading: 511 / out of range -> NULL', COUNT(*)
  FROM ais_position WHERE true_heading IS NOT NULL AND NOT (true_heading BETWEEN 0 AND 359)
UNION ALL
SELECT 'nav_status: 15 / out of range -> NULL', COUNT(*)
  FROM ais_position WHERE nav_status IS NOT NULL AND NOT (nav_status BETWEEN 0 AND 14)
UNION ALL
SELECT 'rate_of_turn: -128 / out of range -> NULL', COUNT(*)
  FROM ais_position WHERE rate_of_turn IS NOT NULL AND NOT (rate_of_turn BETWEEN -127 AND 127)
UNION ALL
SELECT 'position: lat 91 / lon 181 / (0,0) / NULL -> invalid', COUNT(*)
  FROM ais_position_clean WHERE position_valid = 0
UNION ALL
SELECT 'TOTAL raw rows in ais_position', COUNT(*) FROM ais_position;
