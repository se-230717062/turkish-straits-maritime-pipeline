#!/usr/bin/env python3
"""M3 checks on a TEMPORARY database built from synthetic raw files.

Run from the repository root:  python src/checks_m3.py
"""
import json
import sys
import tempfile
from pathlib import Path

import load
from db import ROOT, connect, table_counts, table_digest

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append(ok)
    print("%-26s %s%s" % (name + ":", "PASS" if ok else "FAIL", ("  " + detail) if detail else ""))


def ais(mmsi, lat, lon, sog, ts):
    return {"MessageType": "PositionReport",
            "MetaData": {"MMSI": mmsi, "ShipName": "V%d" % mmsi, "latitude": lat, "longitude": lon,
                         "time_utc": ts + ".000000000 +0000 UTC"},
            "Message": {"PositionReport": {"Latitude": lat, "Longitude": lon, "Sog": sog, "Cog": 90.0,
                                           "TrueHeading": 90, "NavigationalStatus": 1, "RateOfTurn": 0}}}


def build_raw(raw):
    (raw / "ais").mkdir(parents=True)
    (raw / "marine").mkdir()
    recs = [ais(1, 41.27, 29.20, 0.1, "2026-10-11 14:10:00"),   # inside Bosphorus North, rough hour
            ais(2, 41.27, 29.20, 8.0, "2026-10-11 15:10:00"),   # inside, severe hour
            ais(3, 41.60, 29.20, 0.1, "2026-10-11 14:10:00")]   # outside every polygon
    (raw / "ais" / "20261011T160000Z.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    times = ["2026-10-11T%02d:00" % h for h in (13, 14, 15, 16)]
    wave = {"latitude": 41.375, "longitude": 29.208, "utc_offset_seconds": 0,
            "hourly": {"time": times, "wave_height": [1.0, 2.0, 3.0, 1.0],
                       "wave_direction": [0] * 4, "wave_period": [5] * 4, "wind_wave_height": [1] * 4}}
    (raw / "marine" / "20261011T160000Z.json").write_text(json.dumps(wave))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        raw, db = tmp / "raw", tmp / "m3.db"
        build_raw(raw)
        rc1 = load.run(db, raw, out=lambda m: None)
        con = connect(db)
        d1, c1 = table_digest(con), table_counts(con)
        con.close()
        rc2 = load.run(db, raw, out=lambda m: None)
        con = connect(db)
        d2 = table_digest(con)
        report("load + idempotence (M3)", rc1 == 0 and rc2 == 0 and d1 == d2,
               "anchorage=%d, vertices=%d" % (c1["anchorage"], c1["anchorage_vertex"]))

        # sentinel / invalid rows injected as received
        con.execute("INSERT INTO vessel (mmsi, retrieved_at, source_file) VALUES (4, '2026-10-11T16:00:00Z', 't/x')")
        con.execute("INSERT INTO vessel (mmsi, retrieved_at, source_file) VALUES (5, '2026-10-11T16:00:00Z', 't/x')")
        ins = ("INSERT INTO ais_position (vessel_key, observed_at, latitude, longitude, sog, cog, true_heading,"
               " nav_status, rate_of_turn, retrieved_at, source_file) VALUES "
               "((SELECT vessel_key FROM vessel WHERE mmsi = ?), '2026-10-11T14:20:00.000Z', ?, ?, ?, ?, ?, ?, ?,"
               " '2026-10-11T16:00:00Z', 't/x')")
        con.execute(ins, (4, 41.27, 29.20, 102.3, 360.0, 511, 15, -128))   # all sentinels, valid position
        con.execute(ins, (5, 91.0, 181.0, 5.0, 10.0, 10, 0, 0))            # invalid position
        k = {m: v for m, v in con.execute("SELECT mmsi, vessel_key FROM vessel")}

        def clean(m):
            return con.execute("SELECT latitude, sog, cog, true_heading, nav_status, rate_of_turn, position_valid "
                               "FROM ais_position_clean WHERE vessel_key = ?", (k[m],)).fetchone()

        r4, r5, r1 = clean(4), clean(5), clean(1)
        report("sentinels -> NULL", r4[1:6] == (None,) * 5 and r4[6] == 1, str(r4))
        report("valid values kept", r1[1] == 0.1 and r1[2] == 90.0 and r1[3] == 90 and r1[4] == 1, str(r1))
        report("invalid position flagged", r5[0] is None and r5[6] == 0 and r5[1] == 5.0, str(r5))
        raw_row = con.execute("SELECT sog, true_heading, latitude FROM ais_position WHERE vessel_key = ?",
                              (k[4],)).fetchone(), con.execute(
            "SELECT latitude FROM ais_position WHERE vessel_key = ?", (k[5],)).fetchone()
        report("raw table untouched", raw_row[0][:2] == (102.3, 511) and raw_row[1][0] == 91.0)
        log = dict(con.execute("SELECT rule, n_rows FROM cleaning_log"))
        report("cleaning_log counts", log["sog: 102.3 / out of range -> NULL"] == 1
               and log["position: lat 91 / lon 181 / (0,0) / NULL -> invalid"] == 1
               and log["TOTAL raw rows in ais_position"] == 5)

        inside = {m: [a for (a,) in con.execute(
            "SELECT anchorage_key FROM ais_in_anchorage WHERE vessel_key = ?", (k[m],))] for m in k}
        report("point in polygon", inside[1] == [1] and inside[2] == [1] and inside[3] == []
               and inside[5] == [], str(inside))
        dup = con.execute("SELECT COUNT(*) FROM (SELECT 1 FROM ais_in_anchorage "
                          "GROUP BY vessel_key, observed_at HAVING COUNT(*) > 1)").fetchone()[0]
        report("no overlapping polygons", dup == 0)

        rows = con.execute((ROOT / "sql/queries/wave_vs_speed_anchorage.sql").read_text()).fetchall()
        exp = [("Bosphorus North (Black Sea entrance)", "2 rough (1.5-2.5 m)", 1, 1, 0.1, 1, 100.0),
               ("Bosphorus North (Black Sea entrance)", "3 severe (> 2.5 m)", 1, 1, 8.0, 0, 0.0)]
        report("anchorage query result", rows == exp, "" if rows == exp else str(rows))
        old = con.execute((ROOT / "sql/queries/wave_vs_speed.sql").read_text()).fetchall()
        n_old = sum(r[3] for r in old)
        report("M2 query on clean layer", n_old == 3, "positions matched: %d (sentinel-Sog and invalid-position rows excluded)" % n_old)
        con.close()

    ok = all(RESULTS)
    print("\nALL M3 CHECKS PASSED" if ok else "\nSOME M3 CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
