#!/usr/bin/env python3
"""M1 ingestion: AISStream (AIS telemetry) + Open-Meteo Marine (wave state).

Run from the repository root:  python src/ingest.py

Raw responses are saved exactly as received to data/raw/<source>/<UTC timestamp>.<ext>.
No cleaning, filtering or renaming happens here (that is M3).

M5: the run is driven by --run-time (the pipeline's clock; file names and every "since" decision use it).
The marine request heals itself: it asks for everything since the last landed file, plus an overlap, bounded
by what Open-Meteo still serves (past_days <= 92). AISStream is a live stream with no history: a missed
run loses that period for good, and the run says so loudly.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import websocket
from dotenv import load_dotenv

from db import drill, format_run_time, parse_run_time

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

# ---- healing (M5). Each constant is justified in docs/pipeline.md.
OVERLAP_HOURS = int(os.environ.get("OVERLAP_HOURS", "24"))        # re-fetch this much already-landed data: forecasts of past hours get revised
SOURCE_MEMORY_DAYS = 92                                           # Open-Meteo marine: past_days is at most 92; older hours cannot be fetched
FIRST_RUN_PAST_DAYS = int(os.environ.get("FIRST_RUN_PAST_DAYS", "2"))  # no landed file yet: same window as M1
AIS_GAP_WARN_HOURS = float(os.environ.get("AIS_GAP_WARN_HOURS", "3"))  # AIS has no history: warn when the last window is older than this


class IngestError(Exception):
    """Non-recoverable or retries-exhausted failure for one source."""


class AuthError(IngestError):
    """401/403 or invalid API key. Never retried."""


def utc_stamp(run_time: datetime) -> str:
    """File name stamp = the pipeline's clock, never the wall clock."""
    return run_time.strftime("%Y%m%dT%H%M%SZ")


def open_new_file(source: str, ext: str, run_time: datetime):
    """Open a brand-new raw file (mode 'xb' -> refuses to overwrite)."""
    folder = RAW_DIR / source
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{utc_stamp(run_time)}.{ext}"
    try:
        return path, open(path, "xb")
    except FileExistsError:
        raise IngestError(f"a raw file for run time {format_run_time(run_time)} already exists: {path.name}")


def last_landed(source: str, ext: str):
    """Stamp of the newest raw file of a source (a file exists only after a fully successful fetch), or None."""
    folder = RAW_DIR / source
    stamps = []
    for p in folder.glob("*." + ext) if folder.is_dir() else []:
        try:
            stamps.append(datetime.strptime(p.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
        except ValueError:
            raise IngestError(f"raw file name is not a UTC stamp: {p.name}")
    return max(stamps) if stamps else None


def plan_marine_window(run_time, last, overlap_hours=None, memory_days=None, first_run_past_days=None) -> dict:
    """How far back must the marine request reach? Pure function (tested without a network).

    need = (run_time - last) + overlap. Open-Meteo returns whole days, so past_days counts calendar days back
    from the run date. lost=True means the gap is longer than the source's memory: the oldest hours are gone.
    """
    overlap_hours = OVERLAP_HOURS if overlap_hours is None else overlap_hours
    memory_days = SOURCE_MEMORY_DAYS if memory_days is None else memory_days
    first_run_past_days = FIRST_RUN_PAST_DAYS if first_run_past_days is None else first_run_past_days
    if last is None:
        return {"past_days": min(first_run_past_days, memory_days), "gap_hours": None, "needed_days": first_run_past_days, "lost": False}
    gap_hours = max((run_time - last).total_seconds() / 3600.0, 0.0)
    start = run_time - timedelta(hours=gap_hours + overlap_hours)
    needed = max((run_time.date() - start.date()).days, 1)
    return {"past_days": min(needed, memory_days), "gap_hours": gap_hours, "needed_days": needed, "lost": needed > memory_days}


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
def ingest_ais(run_time) -> dict:
    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        raise AuthError("AISSTREAM_API_KEY is not set (see .env.example).")

    lat1, lon1, lat2, lon2 = AIS_BBOX
    subscription = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": [[[lat1, lon1], [lat2, lon2]]],
        "FilterMessageTypes": ["PositionReport"],
    })

    last = last_landed("ais", "jsonl")
    if last is not None and (run_time - last).total_seconds() / 3600.0 > AIS_GAP_WARN_HOURS:
        log.warning("WARNING: no AIS window for %.1f h (last landed %s). AISStream keeps no history: the vessel "
                    "positions of that period are lost for good.", (run_time - last).total_seconds() / 3600.0, format_run_time(last))
    path, fh = open_new_file("ais", "jsonl", run_time)
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
def ingest_marine(run_time) -> dict:
    plan = plan_marine_window(run_time, last_landed("marine", "json"))
    log.info("Marine: gap since last landed file %s h, asking for past_days=%d (overlap %d h)",
             "n/a (first run)" if plan["gap_hours"] is None else "%.1f" % plan["gap_hours"], plan["past_days"], OVERLAP_HOURS)
    params = {
        "latitude": ",".join(str(p[0]) for p in MARINE_POINTS),
        "longitude": ",".join(str(p[1]) for p in MARINE_POINTS),
        "hourly": "wave_height,wave_direction,wave_period,wind_wave_height",
        "past_days": plan["past_days"],
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

    path, fh = open_new_file("marine", "json", run_time)
    with fh:
        fh.write(resp.content)  # exactly as received
    if plan["lost"]:   # the file is landed (everything the source still has), and the run still fails loudly
        raise IngestError("GAP: %.1f h since the last landed file is more than Open-Meteo's %d days of archive; "
                          "the hours before %s cannot be recovered" % (
                              plan["gap_hours"], SOURCE_MEMORY_DAYS, format_run_time(run_time - timedelta(days=SOURCE_MEMORY_DAYS))))
    return {"files": 1, "records": len(locations), "path": str(path.relative_to(ROOT)),
            "null_points": empty_points, "past_days": plan["past_days"]}


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-time", help="the pipeline's clock, e.g. 2026-11-20T08:10Z (default: now)")
    args = ap.parse_args()
    drill("ingest")
    run_time = parse_run_time(args.run_time)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("run time (pipeline clock): %s", format_run_time(run_time))
    sources = [("AISStream (AIS PositionReports)", lambda: ingest_ais(run_time)),
               ("Open-Meteo Marine (wave grid)", lambda: ingest_marine(run_time))]
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