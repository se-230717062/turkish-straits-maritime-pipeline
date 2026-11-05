-- M3 cleaned layer. Same grain, primary key and row count as the M2 table each one cleans.
-- <col>_raw is exactly what M2 loaded, <col> the cleaned value (NULL when not trusted, never imputed),
-- <col>_flag says why they differ. Every table is STRICT; every flag column has a CHECK list.
-- Safe to run more than once.

-- ------------------------------------------------------------- mapping tables (loaded from data/mappings/*.csv)
CREATE TABLE IF NOT EXISTS nav_status_map (
    code         INTEGER PRIMARY KEY,
    label        TEXT NOT NULL,
    motion_group TEXT NOT NULL CHECK (motion_group IN ('moving', 'stationary', 'other')),
    decided_by   TEXT NOT NULL,
    decided_at   TEXT NOT NULL,
    note         TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS name_placeholder (
    name_key   TEXT PRIMARY KEY,          -- normalised key (NFC, Turkish letters folded, casefold)
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    note       TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS vessel_alias (
    alias_mmsi     INTEGER PRIMARY KEY,   -- source identifier (MMSI), not the surrogate key
    canonical_mmsi INTEGER NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('re_registered', 'two_transponders', 'typo')),
    decided_by     TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    note           TEXT NOT NULL,
    CHECK (alias_mmsi <> canonical_mmsi)
) STRICT;

-- ------------------------------------------------------------- cleaned tables
CREATE TABLE IF NOT EXISTS vessel_clean (
    vessel_key      INTEGER PRIMARY KEY REFERENCES vessel (vessel_key),
    mmsi_raw        INTEGER NOT NULL,
    mmsi            INTEGER,
    mmsi_flag       TEXT NOT NULL CHECK (mmsi_flag IN ('ok', 'invalid_length', 'not_a_ship')),
    ship_name_raw   TEXT,
    ship_name       TEXT,
    ship_name_flag  TEXT NOT NULL CHECK (ship_name_flag IN ('ok', 'normalised', 'missing_not_reported', 'placeholder_name'))
) STRICT;

CREATE TABLE IF NOT EXISTS ais_position_clean (
    vessel_key          INTEGER NOT NULL REFERENCES vessel (vessel_key),
    observed_at         TEXT NOT NULL,
    latitude_raw        REAL,
    latitude            REAL,
    latitude_flag       TEXT NOT NULL CHECK (latitude_flag IN ('ok', 'missing_not_reported', 'invalid_sentinel',
                            'invalid_out_of_range', 'invalid_null_island', 'invalid_outside_region')),
    longitude_raw       REAL,
    longitude           REAL,
    longitude_flag      TEXT NOT NULL CHECK (longitude_flag IN ('ok', 'missing_not_reported', 'invalid_sentinel',
                            'invalid_out_of_range', 'invalid_null_island', 'invalid_outside_region')),
    sog_raw             REAL,
    sog                 REAL,
    sog_flag            TEXT NOT NULL CHECK (sog_flag IN ('ok', 'missing_not_reported', 'invalid_sentinel',
                            'invalid_out_of_range', 'unusual_high')),
    cog_raw             REAL,
    cog                 REAL,
    cog_flag            TEXT NOT NULL CHECK (cog_flag IN ('ok', 'missing_not_reported', 'invalid_sentinel',
                            'invalid_out_of_range')),
    true_heading_raw    INTEGER,
    true_heading        INTEGER,
    true_heading_flag   TEXT NOT NULL CHECK (true_heading_flag IN ('ok', 'missing_not_reported', 'invalid_sentinel',
                            'invalid_out_of_range')),
    nav_status_raw      INTEGER,
    nav_status          INTEGER,
    nav_status_flag     TEXT NOT NULL CHECK (nav_status_flag IN ('ok', 'missing_not_reported', 'invalid_sentinel',
                            'invalid_out_of_range')),
    rate_of_turn_raw    INTEGER,
    rate_of_turn        INTEGER,
    rate_of_turn_flag   TEXT NOT NULL CHECK (rate_of_turn_flag IN ('ok', 'missing_not_reported', 'invalid_sentinel',
                            'invalid_out_of_range')),
    row_flag            TEXT NOT NULL CHECK (row_flag IN ('ok', 'nav_status_conflicts_with_sog')),
    PRIMARY KEY (vessel_key, observed_at)
) STRICT;

CREATE TABLE IF NOT EXISTS wave_reading_clean (
    point_key              INTEGER NOT NULL REFERENCES marine_point (point_key),
    observed_at            TEXT NOT NULL,
    wave_height_raw        REAL,
    wave_height            REAL,
    wave_height_flag       TEXT NOT NULL CHECK (wave_height_flag IN ('ok', 'missing_not_reported',
                               'missing_no_model_coverage', 'invalid_negative', 'invalid_out_of_range',
                               'stuck_value', 'spike', 'unusual_high')),
    wave_direction_raw     REAL,
    wave_direction         REAL,
    wave_direction_flag    TEXT NOT NULL CHECK (wave_direction_flag IN ('ok', 'missing_not_reported',
                               'missing_no_model_coverage', 'invalid_out_of_range')),
    wave_period_raw        REAL,
    wave_period            REAL,
    wave_period_flag       TEXT NOT NULL CHECK (wave_period_flag IN ('ok', 'missing_not_reported',
                               'missing_no_model_coverage', 'invalid_out_of_range')),
    wind_wave_height_raw   REAL,
    wind_wave_height       REAL,
    wind_wave_height_flag  TEXT NOT NULL CHECK (wind_wave_height_flag IN ('ok', 'missing_not_reported',
                               'missing_no_model_coverage', 'invalid_negative', 'invalid_out_of_range',
                               'inconsistent_exceeds_total')),
    PRIMARY KEY (point_key, observed_at)
) STRICT;

-- Identity: analyses read vessels through this view (one resolved vessel per MMSI alias decision).
CREATE VIEW IF NOT EXISTS vessel_resolved AS
SELECT v.vessel_key, v.mmsi,
       COALESCE(c.vessel_key, v.vessel_key) AS resolved_vessel_key,
       COALESCE(a.canonical_mmsi, v.mmsi)   AS resolved_mmsi
FROM vessel AS v
LEFT JOIN vessel_alias AS a ON a.alias_mmsi = v.mmsi
LEFT JOIN vessel AS c ON c.mmsi = a.canonical_mmsi;
