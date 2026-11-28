#!/usr/bin/env python3
"""M5 evidence for the overlap: how late does each source's data arrive, and are earlier values revised?

Run from the repository root:  python src/delay_report.py [--raw data/raw]
Reads the landed raw files only (no network, no database). Paste the output into docs/pipeline.md.

Marine: every file is a forecast window. For each reading, delay = file time - hour the reading describes
(negative = a forecast of a future hour). A reading that shows up in several files is a revision candidate:
we compare each file with the next one and report, by how old the hour was when the earlier file fetched it,
how often the value changed. The oldest age at which a change still happened is the minimum overlap.
AIS: positions are real time; the table shows how long after the file's start each position was observed.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import load
from db import ROOT

BUCKETS = [("forecast (hour still ahead)", None, 0), ("0-6 h old", 0, 6), ("6-24 h old", 6, 24), ("24-48 h old", 24, 48), ("48 h or older", 48, None)]


def file_time(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def marine_report(files, out):
    series = defaultdict(list)          # (lat, lon, hour) -> [(file_time, value)]
    delays = []                         # (file stamp, min delay h, max delay h)
    for stamp, path in files:
        ft = file_time(stamp)
        body = json.loads(path.read_bytes())
        lo, hi = None, None
        for loc in body if isinstance(body, list) else [body]:
            off = loc.get("utc_offset_seconds", 0)
            vals = loc["hourly"].get("wave_height") or [None] * len(loc["hourly"]["time"])
            for t, v in zip(loc["hourly"]["time"], vals):
                hour = datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) - timedelta(seconds=off)
                d = (ft - hour).total_seconds() / 3600.0
                lo, hi = (d if lo is None else min(lo, d)), (d if hi is None else max(hi, d))
                if v is not None:
                    series[(loc["latitude"], loc["longitude"], hour)].append((ft, v))
        delays.append((stamp, lo, hi))
    out("MARINE: %d files" % len(files))
    out("  window of each file, in hours relative to the file time (negative = in the past, positive = still ahead):")
    for stamp, lo, hi in delays:
        out("    %s  from %+7.1f h to %+7.1f h" % (stamp, -hi, -lo))
    rows = defaultdict(lambda: [0, 0, 0.0])
    last_changed_age = None
    for (la, lo_, hour), vs in series.items():
        vs.sort(key=lambda x: x[0])
        for (f1, v1), (f2, v2) in zip(vs, vs[1:]):
            age = (f1 - hour).total_seconds() / 3600.0
            for name, a, b in BUCKETS:
                if (a is None or age >= a) and (b is None or age < b):
                    rows[name][0] += 1
                    if v1 != v2:
                        rows[name][1] += 1
                        rows[name][2] = max(rows[name][2], abs(v2 - v1))
                        last_changed_age = age if last_changed_age is None else max(last_changed_age, age)
    out("  revisions: the same (point, hour) in two consecutive files, by the age of the hour at the earlier fetch:")
    out("    %-30s %8s %8s %8s %12s" % ("age at earlier fetch", "pairs", "changed", "share", "max change m"))
    for name, _, _ in BUCKETS:
        n, c, m = rows[name]
        out("    %-30s %8d %8d %7.0f%% %12.2f" % (name, n, c, 100.0 * c / n if n else 0.0, m))
    out("  oldest age at which a value still changed: %s" % ("none seen" if last_changed_age is None else "%.0f h" % last_changed_age))
    out("  -> the overlap must reach back at least that far (OVERLAP_HOURS in ingest.py, now %s)." % "24")


def ais_report(files, out):
    out("AIS: %d files" % len(files))
    worst_lo, worst_hi, n = None, None, 0
    for stamp, path in files:
        ft = file_time(stamp)
        for raw in open(path, "rb"):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("MessageType") != "PositionReport":
                continue
            t = load.parse_ais_time((rec.get("MetaData") or {}).get("time_utc"))
            if t is None:
                continue
            d = (datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) - ft).total_seconds()
            worst_lo, worst_hi, n = (d if worst_lo is None else min(worst_lo, d)), (d if worst_hi is None else max(worst_hi, d)), n + 1
    if n:
        out("  %d positions: observed between %+.0f s and %+.0f s after the file's start stamp" % (n, worst_lo, worst_hi))
        out("  -> AIS arrives in real time and never revised: no overlap, but a missed run cannot be recovered.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", default=str(load.RAW_DIR))
    args = ap.parse_args()
    raw = ROOT / args.raw if not args.raw.startswith("/") else __import__("pathlib").Path(args.raw)
    marine_report(load.list_raw(raw / "marine", "json"), print)
    ais_report(load.list_raw(raw / "ais", "jsonl"), print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
