# Turkish Straits Maritime Pipeline

Real-time AIS vessel telemetry + oceanographic wave data for anchorage congestion analytics around the Turkish Straits.

## The question

When significant wave height exceeds critical thresholds (> 1.5 m / > 2.5 m), do vessels around the Turkish Straits
approach zones reduce their speed over ground and divert into designated anchorage zones, and by how much does
the average waiting duration increase? Vessels with Sog < 0.5 kn inside anchorage polygons are labelled
"Anchored / Waiting", all others "Underway". Rule-based SQL aggregations (by wave-height bucket and anchorage zone)
quantify queue counts, waiting-time distributions and a sea-state disruption index - no ML involved.

## Data sources

Details, licences and known issues: [docs/sources.md](docs/sources.md)

- **AISStream** - live AIS PositionReports via WebSocket (API key required).
- **Open-Meteo Marine Weather API** - hourly wave data via REST (no key).
  Attribution: weather data by [Open-Meteo.com](https://open-meteo.com/), licensed CC BY 4.0.

Raw data is **not committed** (AISStream does not clearly allow redistribution); run the pipeline to regenerate it.

## How to run

```bash
# 1. clone
git clone https://github.com/se-230717062/turkish-straits-maritime-pipeline.git
cd turkish-straits-maritime-pipeline

# 2. environment (Python 3.10+)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. credentials: get a free API key at https://aisstream.io/
cp .env.example .env               # Windows: copy .env.example .env
# edit .env and set AISSTREAM_API_KEY=<your key>

# 4. run (single command, from the repo root)
python src/ingest.py
```

A run collects AIS messages for `AIS_COLLECT_SECONDS` (default 600 s = 10 min), then fetches the wave data,
and prints a run summary. Output lands in `data/raw/ais/<UTC timestamp>.jsonl` and
`data/raw/marine/<UTC timestamp>.json`. Every run creates new files; nothing is overwritten.

Optional variables (see `.env.example`): `AIS_COLLECT_SECONDS`, `AIS_BBOX`, `MARINE_POINTS`, `MAX_ATTEMPTS`, `HTTP_TIMEOUT`.

## Repository structure

```
.
├── README.md
├── .gitignore
├── .env.example        # variable names with placeholders (real .env is git-ignored)
├── requirements.txt
├── data/raw/           # landed raw data (git-ignored, regenerate with ingest.py)
├── src/ingest.py       # ingestion script
└── docs/sources.md     # data source cards
```

## Status

M1 - ingestion complete

## Team

| Name | GitHub |
|---|---|
| Şeyma Büyükaydın | [@se-230717062](https://github.com/se-230717062) |
| Buse Selin Özmen | [@se-230717039](https://github.com/se-230717039) |
| Hüseyin Sefa Endes | [@sefaend](https://github.com/sefaend) |
