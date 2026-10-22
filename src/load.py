#!/usr/bin/env python3
"""M2 loader: landed raw files (data/raw/...) -> SQLite (sql/schema.sql).

Run from the repository root:  python src/load.py
Never calls an API. Idempotent (upserts), atomic (one transaction),
parameterised (placeholders only), latest retrieved_at wins.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from db import ROOT, connect, init_schema, table_counts, transaction

RAW_DIR = ROOT / os.environ.get("RAW_DATA_DIR", "data/raw")
WAVE_VARS = ("wave_height", "wave_direction", "wave_period", "wind_wave_height")


class LoaderError(Exception):
    """Structural problem: abort the whole run (everything rolls back)."""


# ------------------------------------------------------------------ SQL
UPSERT_VESSEL = """
INSERT INTO vessel (mmsi, ship_name, retrieved_at, source_file) VALUES (?, ?, ?, ?)
ON CONFLICT (mmsi) DO UPDATE SET
    ship_name    = COALESCE(excluded.ship_name, vessel.ship_name),
    retrieved_at = excluded.retrieved_at,
    source_file  = excluded.source_file
WHERE excluded.retrieved_at >= vessel.retrieved_at
"""
UPSERT_POSITION = """
INSERT INTO ais_position (vessel_key, observed_at, latitude, longitude, sog, cog,
                          true_heading, nav_status, rate_of_turn, retrieved_at, source_file)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (vessel_key, observed_at) DO UPDATE SET
    latitude = excluded.latitude, longitude = excluded.longitude, sog = excluded.sog,
    cog = excluded.cog, true_heading = excluded.true_heading,
    nav_status = excluded.nav_status, rate_of_turn = excluded.rate_of_turn,
    retrieved_at = excluded.retrieved_at, source_file = excluded.source_file
WHERE excluded.retrieved_at >= ais_position.retrieved_at
"""
UPSERT_POINT = """
INSERT INTO marine_point (latitude, longitude, retrieved_at, source_file) VALUES (?, ?, ?, ?)
ON CONFLICT (latitude, longitude) DO UPDATE SET
    retrieved_at = excluded.retrieved_at, source_file = excluded.source_file
WHERE excluded.retrieved_at >= marine_point.retrieved_at
"""
UPSERT_WAVE = """
INSERT INTO wave_reading (point_key, observed_at, wave_height, wave_direction, wave_period,
                          wind_wave_height, retrieved_at, source_file)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (point_key, observed_at) DO UPDATE SET
    wave_height = excluded.wave_height, wave_direction = excluded.wave_direction,
    wave_period = excluded.wave_period, wind_wave_height = excluded.wind_wave_height,
    retrieved_at = excluded.retrieved_at, source_file = excluded.source_file
WHERE excluded.retrieved_at >= wave_reading.retrieved_at
"""
UPSERT_QUARANTINE = """
INSERT INTO quarantine (source, reason, payload, payload_hash, retrieved_at, source_file)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (source, payload_hash) DO UPDATE SET
    reason = excluded.reason, payload = excluded.payload,
    retrieved_at = excluded.retrieved_at, source_file = excluded.source_file
WHERE excluded.retrieved_at >= quarantine.retrieved_at
"""


# ------------------------------------------------------------------ cleaning
def clean_real(v, stats):
    """Value for a REAL column. Anything that is not a finite number -> None (counted)."""
    if v is None:
        return None
    if isinstance(v, bool):
        stats["non_numeric_to_null"] += 1
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            stats["non_numeric_to_null"] += 1
            return None
        if math.isfinite(f):
            stats["numeric_strings_converted"] += 1
    else:
        stats["non_numeric_to_null"] += 1
        return None
    if not math.isfinite(f):
        stats["non_numeric_to_null"] += 1
        return None
    return f


def clean_int(v, stats):
    f = clean_real(v, stats)
    if f is None:
        return None
    if f != int(f):
        stats["non_numeric_to_null"] += 1
        return None
    return int(f)


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


_AIS_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?(?:\s+([A-Za-z]+))?$"
)


def parse_ais_time(s):
    """'2026-10-10 11:48:30.614308799 +0000 UTC' -> '2026-10-10T11:48:30.614Z' (UTC, ms)."""
    if not isinstance(s, str):
        return None
    m = _AIS_TIME.match(s.strip())
    if not m:
        return None
    date, clock, frac, off, word = m.groups()
    if off is None and (word or "").upper() not in ("UTC", "GMT"):
        return None  # naive time: we refuse to guess the zone
    try:
        dt = datetime.strptime(date + " " + clock, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if off and off != "Z":
        sign = -1 if off[0] == "-" else 1
        digits = off[1:].replace(":", "")
        dt -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    ms = int(((frac or "") + "000")[:3])
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % ms


def parse_marine_time(s, offset_seconds):
    """'2026-10-20T13:00' (+utc_offset_seconds) -> '2026-10-20T13:00Z'."""
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return (dt - timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%MZ")


# ------------------------------------------------------------------ raw files
def list_raw(folder: Path, ext: str):
    """[(retrieved_at ISO, path)] sorted by retrieved_at (never by read order)."""
    out = []
    for p in folder.glob("*." + ext) if folder.is_dir() else []:
        try:
            ts = datetime.strptime(p.stem, "%Y%m%dT%H%M%SZ")
        except ValueError:
            raise LoaderError("raw file name is not a UTC stamp: %s" % p.name)
        out.append((ts.strftime("%Y-%m-%dT%H:%M:%SZ"), p))
    return sorted(out, key=lambda x: x[0])


def quarantine_item(source, reason, payload, retrieved_at, source_file):
    return {
        "source": source, "reason": reason, "payload": payload,
        "hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "retrieved_at": retrieved_at, "source_file": source_file,
    }


def read_ais(files, stats, quarantine):
    positions, vessels = {}, {}
    for retrieved_at, path in files:
        sfile = path.parent.name + "/" + path.name
        with open(path, "rb") as fh:
            for line_no, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                stats["ais_records_read"] += 1
                text = raw.decode("utf-8", errors="replace")
                try:
                    rec = json.loads(raw)
                except ValueError:
                    quarantine.append(quarantine_item("ais", "invalid_json", text, retrieved_at, sfile))
                    continue
                if not isinstance(rec, dict):
                    quarantine.append(quarantine_item("ais", "not_an_object", text, retrieved_at, sfile))
                    continue
                if "error" in rec or rec.get("MessageType") != "PositionReport":
                    stats["non_position_frames_skipped"] += 1  # e.g. SubscriptionConfirmation
                    continue
                meta = rec.get("MetaData")
                msg = (rec.get("Message") or {}).get("PositionReport")
                if not isinstance(meta, dict) or not isinstance(msg, dict):
                    quarantine.append(quarantine_item("ais", "missing_position_report", text, retrieved_at, sfile))
                    continue
                mmsi = meta.get("MMSI")
                if not (isinstance(mmsi, int) and not isinstance(mmsi, bool) and mmsi > 0):
                    ms = meta.get("MMSI_String")
                    mmsi = int(ms) if isinstance(ms, str) and ms.strip().isdigit() and int(ms) > 0 else None
                if mmsi is None:
                    quarantine.append(quarantine_item("ais", "missing_or_invalid_mmsi", text, retrieved_at, sfile))
                    continue
                observed_at = parse_ais_time(meta.get("time_utc"))
                if observed_at is None:
                    quarantine.append(quarantine_item("ais", "unparseable_time_utc", text, retrieved_at, sfile))
                    continue

                lat = msg.get("Latitude", meta.get("latitude"))
                lon = msg.get("Longitude", meta.get("longitude"))
                row = (
                    clean_real(lat, stats), clean_real(lon, stats),
                    clean_real(msg.get("Sog"), stats), clean_real(msg.get("Cog"), stats),
                    clean_int(msg.get("TrueHeading"), stats), clean_int(msg.get("NavigationalStatus"), stats),
                    clean_int(msg.get("RateOfTurn"), stats),
                )
                key, rank = (mmsi, observed_at), (retrieved_at, line_no)
                if key in positions:
                    stats["duplicate_events_collapsed"] += 1
                    if positions[key][0] > rank:
                        continue
                positions[key] = (rank, row, retrieved_at, sfile)

                name = meta.get("ShipName")
                name = name.strip() or None if isinstance(name, str) else None
                vrank = (retrieved_at, observed_at, line_no)
                v = vessels.setdefault(mmsi, {"rank": None, "name_rank": None, "name": None})
                if v["rank"] is None or vrank > v["rank"]:
                    v.update(rank=vrank, retrieved_at=retrieved_at, source_file=sfile)
                if name and (v["name_rank"] is None or vrank > v["name_rank"]):
                    v.update(name=name, name_rank=vrank)
    return vessels, positions


def write_ais(con, vessels, positions, stats):
    existing = {m: (n, r) for m, n, r in con.execute("SELECT mmsi, ship_name, retrieved_at FROM vessel")}
    for mmsi, v in vessels.items():
        old = existing.get(mmsi)
        if old and old[0] and v["name"] and old[0] != v["name"] and v["retrieved_at"] >= old[1]:
            stats["ship_names_changed"] += 1
    con.executemany(UPSERT_VESSEL, [
        (m, vessels[m]["name"], vessels[m]["retrieved_at"], vessels[m]["source_file"])
        for m in sorted(vessels)
    ])
    keys = dict(con.execute("SELECT mmsi, vessel_key FROM vessel"))
    con.executemany(UPSERT_POSITION, [
        (keys[m], t) + positions[(m, t)][1] + (positions[(m, t)][2], positions[(m, t)][3])
        for (m, t) in sorted(positions)
    ])


def read_marine(files, stats, quarantine):
    points, waves = {}, {}
    for retrieved_at, path in files:
        sfile = path.parent.name + "/" + path.name
        try:
            body = json.loads(path.read_bytes())
        except ValueError:
            raise LoaderError("%s: not valid JSON" % sfile)
        locs = body if isinstance(body, list) else [body]
        for idx, loc in enumerate(locs):
            if not isinstance(loc, dict) or loc.get("error"):
                raise LoaderError("%s: unexpected structure / API error object" % sfile)
            stats["marine_locations_read"] += 1
            lat, lon = loc.get("latitude"), loc.get("longitude")
            if not (is_number(lat) and is_number(lon)):
                quarantine.append(quarantine_item(
                    "marine", "missing_or_invalid_coordinates",
                    json.dumps(loc, sort_keys=True)[:20000], retrieved_at, sfile))
                continue
            hourly = loc.get("hourly")
            if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
                raise LoaderError("%s: location %d has no hourly time series" % (sfile, idx))
            offset = loc.get("utc_offset_seconds", 0)
            if not isinstance(offset, int) or isinstance(offset, bool):
                raise LoaderError("%s: bad utc_offset_seconds" % sfile)
            times = hourly["time"]
            series = {}
            for var in WAVE_VARS:
                vals = hourly.get(var)
                if vals is None:
                    vals = [None] * len(times)
                if not isinstance(vals, list) or len(vals) != len(times):
                    raise LoaderError("%s: '%s' length does not match 'time'" % (sfile, var))
                series[var] = vals
            pkey = (float(lat), float(lon))
            rank = (retrieved_at, idx)
            if pkey not in points or rank >= points[pkey][0]:
                points[pkey] = (rank, retrieved_at, sfile)
            for i, t in enumerate(times):
                observed_at = parse_marine_time(t, offset)
                if observed_at is None:
                    quarantine.append(quarantine_item(
                        "marine", "unparseable_time",
                        json.dumps({"latitude": lat, "longitude": lon, "time": t,
                                    "values": [series[v][i] for v in WAVE_VARS]}, sort_keys=True),
                        retrieved_at, sfile))
                    continue
                stats["marine_readings_read"] += 1
                vals = tuple(clean_real(series[v][i], stats) for v in WAVE_VARS)
                stats["wave_values_null_in_source"] += sum(1 for v in series.values() if v[i] is None)
                key = (pkey, observed_at)
                if key in waves:
                    stats["duplicate_events_collapsed"] += 1
                    if waves[key][0] > rank:
                        continue
                waves[key] = (rank, vals, retrieved_at, sfile)
    return points, waves


def write_marine(con, points, waves):
    con.executemany(UPSERT_POINT, [
        (lat, lon, points[(lat, lon)][1], points[(lat, lon)][2]) for (lat, lon) in sorted(points)
    ])
    keys = {(la, lo): k for k, la, lo in con.execute("SELECT point_key, latitude, longitude FROM marine_point")}
    con.executemany(UPSERT_WAVE, [
        (keys[p], t) + waves[(p, t)][1] + (waves[(p, t)][2], waves[(p, t)][3])
        for (p, t) in sorted(waves)
    ])


def write_quarantine(con, items):
    con.executemany(UPSERT_QUARANTINE, [
        (q["source"], q["reason"], q["payload"], q["hash"], q["retrieved_at"], q["source_file"])
        for q in sorted(items, key=lambda q: (q["retrieved_at"], q["source"], q["hash"]))
    ])


# ------------------------------------------------------------------ run
def run(db_path=None, raw_dir=None, out=print) -> int:
    raw_dir = Path(raw_dir) if raw_dir else RAW_DIR
    stats, quarantine = Counter(), []
    con = connect(db_path)
    try:
        ais_files = list_raw(raw_dir / "ais", "jsonl")
        marine_files = list_raw(raw_dir / "marine", "json")
        if not ais_files and not marine_files:
            raise LoaderError("no raw files found under %s (run src/ingest.py first)" % raw_dir)
        with transaction(con):
            init_schema(con)
            before = table_counts(con)
            if ais_files:
                vessels, positions = read_ais(ais_files, stats, quarantine)
                write_ais(con, vessels, positions, stats)
            if marine_files:
                points, waves = read_marine(marine_files, stats, quarantine)
                write_marine(con, points, waves)
            write_quarantine(con, quarantine)
            if con.execute("PRAGMA foreign_key_check").fetchall():
                raise LoaderError("foreign_key_check found violations")
            after = table_counts(con)
    except (LoaderError, sqlite3.Error, OSError) as exc:
        out("ERROR: %s" % exc)
        out("STATUS: FAILED (transaction rolled back, database unchanged)")
        return 1
    finally:
        con.close()

    stamps = [t for t, _ in ais_files + marine_files]
    d = {k: after[k] - before[k] for k in after}
    out("\n===== LOAD SUMMARY =====")
    out("files read: %d raw files (ais: %d, marine: %d) (%s .. %s)" % (
        len(stamps), len(ais_files), len(marine_files), min(stamps)[:10], max(stamps)[:10]))
    out("vessels: {:,} in database ({:+d} new this run)".format(after["vessel"], d["vessel"]))
    out("marine_points: {:,} in database ({:+d} new this run)".format(after["marine_point"], d["marine_point"]))
    out("ais_positions: {:,} in database ({:+d} this run)".format(after["ais_position"], d["ais_position"]))
    out("wave_readings: {:,} in database ({:+d} this run)".format(after["wave_reading"], d["wave_reading"]))
    out("records read: ais %d, marine readings %d; duplicates collapsed: %d; non-position frames skipped: %d" % (
        stats["ais_records_read"], stats["marine_readings_read"],
        stats["duplicate_events_collapsed"], stats["non_position_frames_skipped"]))
    out("values cleaned: %d (non-numeric -> NULL); numeric strings converted: %d; NULL in source (kept): %d" % (
        stats["non_numeric_to_null"], stats["numeric_strings_converted"], stats["wave_values_null_in_source"]))
    out("quarantined: %d records this run (%d in table)%s" % (
        len({(q["source"], q["hash"]) for q in quarantine}), after["quarantine"],
        "".join("\n    %s/%s" % (q["source"], q["reason"]) + "" for q in
                {(q["source"], q["reason"]): q for q in quarantine}.values())))
    if before["vessel"] and d["vessel"]:
        out("NOTE: %d new vessel(s) appeared in a non-empty database" % d["vessel"])
    if before["marine_point"] and d["marine_point"]:
        out("WARNING: %d NEW marine point(s) - new grid cell or a changed coordinate?" % d["marine_point"])
    if stats["ship_names_changed"]:
        out("NOTE: %d vessel name(s) changed (type 1: latest kept)" % stats["ship_names_changed"])
    out("foreign_key_check: OK")
    out("STATUS: COMPLETE")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="database path (default data/maritime.db)")
    ap.add_argument("--raw", help="raw data dir (default data/raw)")
    args = ap.parse_args()
    return run(args.db, args.raw)


if __name__ == "__main__":
    sys.exit(main())
