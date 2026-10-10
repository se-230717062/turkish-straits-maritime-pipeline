#!/usr/bin/env python3
"""M1 ingestion: AISStream (AIS telemetry) + Open-Meteo Marine (wave state).

Run from the repository root:  python src/ingest.py

Raw responses are saved exactly as received to data/raw/<source>/<UTC timestamp>.<ext>.
No cleaning, filtering or renaming happens here (that is M3).
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import websocket
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

log = logging.getLogger("ingest")

# ---------------------------------------------------------------- configuration
AIS_URL = "wss://stream.aisstream.io/v0/stream"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
DEFAULT_POINTS = (
    "41.25,29.20;40.95,29.00;40.75,28.00;40.60,27.00;"
    "40.40,26.70;40.00,26.10;40.80,29.50;38.50,26.00"
)

RAW_DIR = ROOT / os.environ.get("RAW_DATA_DIR", "data/raw")
AIS_COLLECT_SECONDS = int(os.environ.get("AIS_COLLECT_SECONDS", "600"))
AIS_BBOX = [float(x) for x in os.environ.get("AIS_BBOX", "35.0,25.0,42.5,36.0").split(",")]
MARINE_POINTS = [
    tuple(float(v) for v in p.split(","))
    for p in os.environ.get("MARINE_POINTS", DEFAULT_POINTS).split(";")
    if p.strip()
]
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
BACKOFF_BASE = 2  # seconds; delay = BACKOFF_BASE * 2**(attempt-1)


class IngestError(Exception):
    """Non-recoverable or retries-exhausted failure for one source."""


class AuthError(IngestError):
    """401/403 or invalid API key. Never retried."""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def open_new_file(source: str, ext: str):
    """Open a brand-new raw file (mode 'xb' -> refuses to overwrite)."""
    folder = RAW_DIR / source
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{utc_stamp()}.{ext}"
    return path, open(path, "xb")


# ---------------------------------------------------------------- HTTP helper
def request_with_retry(session: requests.Session, url: str, params: dict) -> requests.Response:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        retry_after = None
        try:
            resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            reason = f"network error: {exc!r}"
        else:
            if resp.status_code in (401, 403):
                raise AuthError(f"Authentication failed (HTTP {resp.status_code}). Check your credentials.")
            if resp.status_code == 429 or resp.status_code >= 500:
                reason = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
            elif resp.ok:
                return resp
            else:
                raise IngestError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        if attempt == MAX_ATTEMPTS:
            raise IngestError(f"Giving up after {MAX_ATTEMPTS} attempts ({reason})")
        delay = BACKOFF_BASE * 2 ** (attempt - 1)
        if retry_after and retry_after.isdigit():
            delay = max(delay, int(retry_after))
        log.warning("%s - retry %d/%d in %ds", reason, attempt, MAX_ATTEMPTS - 1, delay)
        time.sleep(delay)
    raise IngestError("unreachable")


# ---------------------------------------------------------------- source 1: AISStream
def ingest_ais() -> dict:
    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        raise AuthError("AISSTREAM_API_KEY is not set (see .env.example).")

    lat1, lon1, lat2, lon2 = AIS_BBOX
    subscription = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": [[[lat1, lon1], [lat2, lon2]]],
        "FilterMessageTypes": ["PositionReport"],
    })

    path, fh = open_new_file("ais", "jsonl")
    deadline = time.monotonic() + AIS_COLLECT_SECONDS
    count = 0
    failures = 0
    try:
        with fh:
            while time.monotonic() < deadline:
                ws = None
                try:
                    ws = websocket.create_connection(AIS_URL, timeout=15)
                    ws.send(subscription)
                    ws.settimeout(15)
                    log.info("AIS: connected, collecting until deadline")
                    while time.monotonic() < deadline:
                        try:
                            msg = ws.recv()
                        except websocket.WebSocketTimeoutException:
                            continue  # no traffic in this window; keep waiting
                        if isinstance(msg, bytes):
                            raw = msg
                        else:
                            raw = msg.encode("utf-8")
                        # inspect (not modify) to catch error frames
                        try:
                            parsed = json.loads(raw)
                        except ValueError:
                            parsed = None
                        if isinstance(parsed, dict) and "error" in parsed:
                            err = str(parsed["error"])
                            if "key" in err.lower() or "auth" in err.lower():
                                raise AuthError(f"AISStream rejected the API key: {err}")
                            raise IngestError(f"AISStream error: {err}")
                        fh.write(raw + b"\n")  # saved exactly as received
                        count += 1
                    failures = 0
                except (websocket.WebSocketException, ConnectionError, OSError) as exc:
                    failures += 1
                    if failures >= MAX_ATTEMPTS:
                        raise IngestError(f"AIS stream: giving up after {MAX_ATTEMPTS} reconnect attempts ({exc!r})")
                    delay = BACKOFF_BASE * 2 ** (failures - 1)
                    log.warning("AIS: connection problem %r - reconnect %d/%d in %ds",
                                exc, failures, MAX_ATTEMPTS - 1, delay)
                    time.sleep(delay)
                finally:
                    if ws is not None:
                        try:
                            ws.close()
                        except Exception:
                            pass
    except IngestError:
        if count == 0 and path.exists():
            path.unlink()  # remove the empty file we just created
        raise

    if count == 0:
        path.unlink(missing_ok=True)
        raise IngestError("AIS stream connected but delivered 0 messages (empty result).")
    return {"files": 1, "records": count, "path": str(path.relative_to(ROOT))}


# ---------------------------------------------------------------- source 2: Open-Meteo Marine
def ingest_marine() -> dict:
    params = {
        "latitude": ",".join(str(p[0]) for p in MARINE_POINTS),
        "longitude": ",".join(str(p[1]) for p in MARINE_POINTS),
        "hourly": "wave_height,wave_direction,wave_period,wind_wave_height",
        "past_days": 1,
        "forecast_days": 1,
        "timezone": "UTC",
    }
    with requests.Session() as session:
        resp = request_with_retry(session, MARINE_URL, params)

    # Validate (parse a copy; the saved bytes stay untouched).
    try:
        body = resp.json()
    except ValueError:
        raise IngestError("Open-Meteo returned a non-JSON body.")
    locations = body if isinstance(body, list) else [body]
    if not locations or any(not isinstance(x, dict) for x in locations):
        raise IngestError("Open-Meteo returned an unexpected structure.")
    if any(x.get("error") for x in locations):
        raise IngestError(f"Open-Meteo reported an error: {locations[0].get('reason')}")
    empty_points = 0
    for loc in locations:
        hourly = loc.get("hourly") or {}
        if not hourly.get("time"):
            raise IngestError("Open-Meteo response has no hourly time series (empty result).")
        if all(v is None for v in hourly.get("wave_height", [None])):
            empty_points += 1
    if empty_points:
        log.warning("Marine: %d/%d points returned only null wave_height (no model coverage there)",
                    empty_points, len(locations))

    path, fh = open_new_file("marine", "json")
    with fh:
        fh.write(resp.content)  # exactly as received
    return {"files": 1, "records": len(locations), "path": str(path.relative_to(ROOT)),
            "null_points": empty_points}


# ---------------------------------------------------------------- main
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sources = [("AISStream (AIS PositionReports)", ingest_ais),
               ("Open-Meteo Marine (wave grid)", ingest_marine)]
    results, failed = [], []

    for name, fn in sources:
        log.info("Fetching: %s", name)
        try:
            results.append((name, fn()))
        except AuthError as exc:
            log.error("%s: %s", name, exc)
            failed.append((name, f"AUTH: {exc}"))
            break  # auth failure: stop immediately, do not continue
        except IngestError as exc:
            log.error("%s: %s", name, exc)
            failed.append((name, str(exc)))

    print("\n===== RUN SUMMARY =====")
    for name, r in results:
        print(f"OK     {name}: {r['files']} file(s), {r['records']} record(s) -> {r['path']}")
    for name, err in failed:
        print(f"FAILED {name}: {err}")
    print("STATUS:", "HEALTHY" if not failed else "UNHEALTHY")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())