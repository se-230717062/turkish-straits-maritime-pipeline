# Turkish Straits Maritime Pipeline

Pipeline for ingesting AIS vessel positions and co-located marine weather observations across the Turkish Straits (Bosporus & Dardanelles).

## Data Sources & Licensing

- **AISStream**: Real-time AIS vessel position data stream via WebSocket. Data provided under AISStream Developer API terms of use for non-commercial research and educational purposes.
- **Open-Meteo**: Marine weather observations via REST API. Data licensed under Creative Commons Attribution 4.0 International (CC BY 4.0).

## Instructions

1. Copy `.env.example` to `.env` and supply your `AISSTREAM_API_KEY`.
2. Install dependencies: `pip install -r requirements.txt`.
3. Run the ingestion pipeline: `python src/ingest.py`.
