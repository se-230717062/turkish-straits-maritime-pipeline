-- M2 schema: AIS vessel telemetry + Open-Meteo wave grid.
-- Safe to run more than once. Every table is STRICT.
-- Timestamps: TEXT, ISO 8601 UTC ('2026-10-11T14:00Z' or with seconds/ms).

-- ------------------------------------------------------------- entities
CREATE TABLE IF NOT EXISTS vessel (
    vessel_key   INTEGER PRIMARY KEY,
    mmsi         INTEGER NOT NULL UNIQUE,          -- source identifier
    ship_name    TEXT,                             -- latest known (type 1)
    retrieved_at TEXT NOT NULL
        CHECK (retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]*Z'),
    source_file  TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS marine_point (
    point_key    INTEGER PRIMARY KEY,
    latitude     REAL NOT NULL,                    -- grid cell as returned by Open-Meteo
    longitude    REAL NOT NULL,
    retrieved_at TEXT NOT NULL
        CHECK (retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]*Z'),
    source_file  TEXT NOT NULL,
    UNIQUE (latitude, longitude)                   -- source identifier
) STRICT;

-- ------------------------------------------------------------- events
CREATE TABLE IF NOT EXISTS ais_position (
    vessel_key     INTEGER NOT NULL REFERENCES vessel (vessel_key),
    observed_at    TEXT NOT NULL
        CHECK (observed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]*Z'),
    latitude       REAL,
    longitude      REAL,
    sog            REAL,       -- speed over ground (knots), raw (102.3 = n/a is NOT cleaned here)
    cog            REAL,       -- course over ground, raw
    true_heading   INTEGER,    -- raw (511 = n/a is NOT cleaned here)
    nav_status     INTEGER,
    rate_of_turn   INTEGER,
    retrieved_at   TEXT NOT NULL
        CHECK (retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]*Z'),
    source_file    TEXT NOT NULL,
    PRIMARY KEY (vessel_key, observed_at)
) STRICT;

CREATE INDEX IF NOT EXISTS ix_ais_position_time ON ais_position (observed_at);

CREATE TABLE IF NOT EXISTS wave_reading (
    point_key         INTEGER NOT NULL REFERENCES marine_point (point_key),
    observed_at       TEXT NOT NULL
        CHECK (observed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]*Z'),
    wave_height       REAL,    -- metres; NULL = model has no value (not zero)
    wave_direction    REAL,
    wave_period       REAL,
    wind_wave_height  REAL,
    retrieved_at      TEXT NOT NULL
        CHECK (retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]*Z'),
    source_file       TEXT NOT NULL,
    PRIMARY KEY (point_key, observed_at)
) STRICT;

-- ------------------------------------------------------------- quarantine
-- Records that cannot be loaded (no usable key / unparseable). Kept verbatim.
CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_key INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,          -- 'ais' | 'marine'
    reason         TEXT NOT NULL,
    payload        TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    retrieved_at   TEXT NOT NULL
        CHECK (retrieved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]*Z'),
    source_file    TEXT NOT NULL,
    UNIQUE (source, payload_hash)
) STRICT;
