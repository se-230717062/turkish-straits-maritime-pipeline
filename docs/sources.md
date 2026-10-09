# Data Source Cards

## 1. AISStream - live AIS vessel telemetry

```
source_name: AISStream - live AIS PositionReport stream
provider: AISStream (https://aisstream.io/)
url: https://aisstream.io/documentation
access_method: WebSocket streaming API (wss://stream.aisstream.io/v0/stream), API key required
licence: none stated   # TODO: re-check https://aisstream.io terms before submitting
terms_notes: No explicit redistribution permission found; raw data is therefore NOT committed (data/raw/ is gitignored).
             Service is offered as a free beta; no uptime/completeness guarantee.
update_cadence: continuous / real time
coverage: live only (no history). Bounding box [35.0,25.0]-[42.5,36.0] (Marmara, Aegean, E. Mediterranean)
record_meaning: one row = one PositionReport broadcast by one vessel (MMSI) at one moment
join_key: (latitude, longitude) -> nearest marine grid cell, and time_utc (hour) (joins to: Open-Meteo marine)
first_retrieved: TODO - fill with the timestamp of your first real run (UTC), e.g. 2026-10-12T09:12:44Z
known_issues:
  - Live-only stream: gaps appear whenever the script is not running; no back-fill possible.
  - Terrestrial AIS coverage is uneven; some areas may have few messages.
  - Messages can be duplicated or arrive out of order; Sog = 102.3 means "not available"; Cog/heading have sentinel values.
  - Each run collects only AIS_COLLECT_SECONDS of traffic (default 600 s), so volume per run is a sample.
```

## 2. Open-Meteo Marine Weather API

```
source_name: Open-Meteo Marine Weather - hourly wave variables
provider: Open-Meteo (https://open-meteo.com/)
url: https://open-meteo.com/en/docs/marine-weather-api
access_method: REST API (JSON), no API key
licence: CC BY 4.0 (data); free API tier for non-commercial use   # TODO: verify at https://open-meteo.com/en/license
terms_notes: Attribution to Open-Meteo required (see README). Free tier has rate limits (HTTP 429 handled with backoff).
update_cadence: model updated several times per day; hourly time series
coverage: global ocean grid; requested: past 1 day + today (UTC) for 8 grid points around the Turkish Straits
record_meaning: one row = one hourly value set (wave_height, wave_direction, wave_period, wind_wave_height) at one grid point
join_key: grid point (lat, lon) + hourly time (joins to: AISStream positions after spatial binning)
first_retrieved: TODO - fill with the timestamp of your first real run (UTC)
known_issues:
  - Coarse grid; small/enclosed seas (Sea of Marmara, Bosphorus, Dardanelles) may return null wave values.
    The ingest script logs a warning when a point returns only nulls (raw file is kept as is).
  - Values are model output, not buoy measurements.
  - Multi-location requests return a JSON list (one object per point) in the same order as requested.
```
