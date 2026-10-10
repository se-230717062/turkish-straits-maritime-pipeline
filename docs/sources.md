# Data Source Cards

## 1. AISStream - live AIS vessel telemetry

```
source_name: AISStream - live AIS PositionReport stream
provider: AISStream (https://aisstream.io/)
url: https://aisstream.io/documentation
access_method: WebSocket streaming API (wss://stream.aisstream.io/v0/stream), API key required
licence: none stated (no licence or terms of use found on aisstream.io as of 2026-10-10)
terms_notes: No explicit permission to redistribute found; raw data is therefore NOT committed (data/raw/ is gitignored).
             Pipeline can regenerate the data with a personal API key. Free beta service; no uptime/completeness guarantee.
update_cadence: continuous / real time
coverage: live only (no history). Bounding box [35.0,25.0]-[42.5,36.0] (Marmara, Aegean, E. Mediterranean)
record_meaning: one row = one PositionReport broadcast by one vessel (MMSI) at one moment
join_key: (latitude, longitude) -> nearest marine grid cell, and time_utc (hour) (joins to: Open-Meteo marine)
first_retrieved: 2026-10-10T11:48:26Z
known_issues:
  - Live-only stream: no history; gaps appear whenever the script is not running.
  - First line of every raw file is a SubscriptionConfirmation message, not a vessel record (filter by MessageType in M3).
  - ShipName has trailing whitespace padding (e.g. "SAONISOS            "); needs trimming in M3.
  - Test run (30 s) returned only 14 PositionReports; volume per run depends on AIS_COLLECT_SECONDS (default 600 s).
  - Terrestrial AIS coverage is uneven; Sog = 102.3 means "not available"; messages may be duplicated or out of order.
```

## 2. Open-Meteo Marine Weather API

```
source_name: Open-Meteo Marine Weather - hourly wave variables
provider: Open-Meteo (https://open-meteo.com/)
url: https://open-meteo.com/en/docs/marine-weather-api
access_method: REST API (JSON), no API key
licence: CC BY 4.0 (data); free API for non-commercial use only
terms_notes: Attribution required ("Weather data by Open-Meteo.com", with link to https://open-meteo.com/); see README.
             Free API restricted to non-commercial use (this is a course project). Terms: https://open-meteo.com/en/terms
             Free tier has rate limits (HTTP 429 handled with backoff in ingest.py).
update_cadence: model updated several times per day; hourly time series
coverage: global ocean grid; requested: past 1 day + today (UTC) for 8 grid points around the Turkish Straits
record_meaning: one row = one hourly value set (wave_height, wave_direction, wave_period, wind_wave_height) at one grid point
join_key: grid point (lat, lon) + hourly time (joins to: AISStream positions after spatial binning)
first_retrieved: 2026-10-10T11:48:58Z
known_issues:
  - API snaps requested coordinates to its own grid (requested 41.25,29.20 -> returned 41.375,29.208); use the returned latitude/longitude as join key in M2.
  - First run: all 8 points returned complete wave_height series (48/48 hourly values), including Marmara points.
  - Values are coarse-grid model output, not buoy measurements; wave values in narrow straits may be unrealistic.
  - Multi-location request returns a JSON list in the same order as requested.
```
