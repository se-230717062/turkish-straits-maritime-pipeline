#!/usr/bin/env python3
"""Section 2.2: test the keys on the landed raw data (run before/after schema decisions).

Run from the repository root:  python src/key_tests.py
"""
import json
from collections import defaultdict, Counter

import load

def main():
    stats, q = Counter(), []
    ais_files = load.list_raw(load.RAW_DIR / "ais", "jsonl")
    groups = defaultdict(set)      # (mmsi, observed_at) -> distinct payloads
    names = defaultdict(set)       # mmsi -> distinct ship names
    total = unresolved = 0
    for _, path in ais_files:
        for raw in open(path, "rb"):
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if r.get("MessageType") != "PositionReport":
                continue
            m, p = r.get("MetaData", {}), r.get("Message", {}).get("PositionReport", {})
            t = load.parse_ais_time(m.get("time_utc"))
            if not m.get("MMSI") or t is None:
                unresolved += 1
                continue
            total += 1
            groups[(m["MMSI"], t)].add(json.dumps(p, sort_keys=True))
            if (m.get("ShipName") or "").strip():
                names[m["MMSI"]].add(m["ShipName"].strip())
    print("AIS (mmsi, observed_at): %d records, %d distinct keys" % (total, len(groups)))
    print("  keys seen more than once with DIFFERENT content: %d" % sum(len(v) > 1 for v in groups.values()))
    print("  mmsi with >1 distinct ship name: %d of %d" % (sum(len(v) > 1 for v in names.values()), len(names)))
    print("  records without usable key (-> quarantine): %d" % unresolved)

    pts, vals = defaultdict(set), defaultdict(set)
    for _, path in load.list_raw(load.RAW_DIR / "marine", "json"):
        body = json.loads(path.read_bytes())
        for loc in body if isinstance(body, list) else [body]:
            pk = (loc["latitude"], loc["longitude"])
            h = loc["hourly"]
            for i, t in enumerate(h["time"]):
                vals[(pk, t)].add(tuple(h[v][i] for v in load.WAVE_VARS))
            pts[pk].add(path.name)
    print("Marine points: %d distinct (latitude, longitude)" % len(pts))
    print("Marine (point, time): %d keys; seen in several files with DIFFERENT values (forecast revisions): %d"
          % (len(vals), sum(len(v) > 1 for v in vals.values())))

if __name__ == "__main__":
    main()
