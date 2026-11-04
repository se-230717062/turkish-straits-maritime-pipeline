-- M3 anchorage model. This file is the source of truth for the polygons: they are reloaded on
-- every run (DELETE + INSERT), so editing a vertex here and re-running load.py updates the database.
-- !! The coordinates below are APPROXIMATE PLACEHOLDER boxes around our wave-grid points.
-- !! Replace them with the official anchorage coordinates (nautical chart / port authority).
-- Vertices are an open ring (the last vertex is NOT repeated), listed in order (seq).
-- Polygons must not overlap, otherwise a position would be counted in two anchorages.
CREATE TABLE IF NOT EXISTS anchorage (
    anchorage_key INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    note          TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS anchorage_vertex (
    anchorage_key INTEGER NOT NULL REFERENCES anchorage (anchorage_key),
    seq           INTEGER NOT NULL CHECK (seq >= 0),
    latitude      REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude     REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    PRIMARY KEY (anchorage_key, seq)
) STRICT;

DELETE FROM anchorage_vertex;
DELETE FROM anchorage;

INSERT INTO anchorage (anchorage_key, name, note) VALUES
    (1, 'Bosphorus North (Black Sea entrance)', 'placeholder box'),
    (2, 'Bosphorus South (Istanbul / Marmara)',  'placeholder box'),
    (3, 'Canakkale South (Aegean entrance)',     'placeholder box'),
    (4, 'Izmit Bay entrance',                    'placeholder box');

INSERT INTO anchorage_vertex (anchorage_key, seq, latitude, longitude) VALUES
    (1, 0, 41.20, 29.10), (1, 1, 41.20, 29.30), (1, 2, 41.35, 29.30), (1, 3, 41.35, 29.10),
    (2, 0, 40.88, 28.92), (2, 1, 40.88, 29.05), (2, 2, 40.98, 29.05), (2, 3, 40.98, 28.92),
    (3, 0, 39.93, 25.95), (3, 1, 39.93, 26.15), (3, 2, 40.03, 26.15), (3, 3, 40.03, 25.95),
    (4, 0, 40.74, 29.45), (4, 1, 40.74, 29.65), (4, 2, 40.82, 29.65), (4, 3, 40.82, 29.45);

-- Polygon edges (closing edge included).
DROP VIEW IF EXISTS anchorage_edge;
CREATE VIEW anchorage_edge AS
SELECT a.anchorage_key, a.latitude AS y1, a.longitude AS x1, b.latitude AS y2, b.longitude AS x2
FROM anchorage_vertex AS a
JOIN anchorage_vertex AS b
  ON b.anchorage_key = a.anchorage_key
 AND b.seq = CASE WHEN a.seq = (SELECT MAX(v.seq) FROM anchorage_vertex AS v
                                WHERE v.anchorage_key = a.anchorage_key)
                  THEN 0 ELSE a.seq + 1 END;

-- Point-in-polygon by ray casting: cast a ray towards +longitude, count the edges it crosses.
-- Odd count = inside. Grain: one row per (position, anchorage) the position lies in.
DROP VIEW IF EXISTS ais_in_anchorage;
CREATE VIEW ais_in_anchorage AS
SELECT p.vessel_key, p.observed_at, e.anchorage_key
FROM ais_position_clean AS p
JOIN anchorage_edge AS e
  ON (e.y1 > p.latitude) <> (e.y2 > p.latitude)
 AND p.longitude < (e.x2 - e.x1) * (p.latitude - e.y1) / (e.y2 - e.y1) + e.x1
WHERE p.position_valid = 1
GROUP BY p.vessel_key, p.observed_at, e.anchorage_key
HAVING COUNT(*) % 2 = 1;

-- Each anchorage uses exactly ONE wave point: the nearest to its centroid, within ~0.5 degree.
-- Longitude is scaled by cos(40 deg)^2 = 0.587 so that degrees of longitude and latitude
-- are comparable at this latitude (M2 used raw degrees).
DROP VIEW IF EXISTS anchorage_wave_point;
CREATE VIEW anchorage_wave_point AS
WITH c AS (
    SELECT anchorage_key, AVG(latitude) AS clat, AVG(longitude) AS clon
    FROM anchorage_vertex GROUP BY anchorage_key
), d AS (
    SELECT c.anchorage_key, m.point_key,
           (c.clat - m.latitude) * (c.clat - m.latitude)
         + 0.587 * (c.clon - m.longitude) * (c.clon - m.longitude) AS dist2,
           ROW_NUMBER() OVER (PARTITION BY c.anchorage_key
                              ORDER BY (c.clat - m.latitude) * (c.clat - m.latitude)
                                     + 0.587 * (c.clon - m.longitude) * (c.clon - m.longitude),
                                       m.point_key) AS rn
    FROM c CROSS JOIN marine_point AS m
)
SELECT anchorage_key, point_key, dist2 FROM d WHERE rn = 1 AND dist2 <= 0.25;
