#!/usr/bin/env python3
"""M3 checks on a TEMPORARY database with synthetic M2 rows (every flag is exercised on purpose).

Run from the repository root:  python src/checks_m3.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

import clean
import validate
from db import ROOT, connect, init_schema, table_digest

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append(ok)
    print("%-34s %s%s" % (name + ":", "PASS" if ok else "FAIL", ("  " + detail) if detail else ""))


def make_db(path):
    con = connect(path)
    init_schema(con)
    ts = "2026-10-11T16:00:00Z"
    for mmsi, name in [(271000001, "ALPHA ONE"), (271000002, "@@@@@@@@@@"), (271000003, "I\u0307STANBUL"),
                       (271000004, "istanbul"), (12345, "SHORT MMSI"), (970000001, "SART 1"), (271000007, "unknown")]:
        con.execute("INSERT INTO vessel (mmsi, ship_name, retrieved_at, source_file) VALUES (?, ?, ?, 't/x')",
                    (mmsi, name, ts))
    vk = {m: k for m, k in con.execute("SELECT mmsi, vessel_key FROM vessel")}
    ins = ("INSERT INTO ais_position (vessel_key, observed_at, latitude, longitude, sog, cog, true_heading, "
           "nav_status, rate_of_turn, retrieved_at, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 't/x')")
    pos = [(271000001, "14:10", 41.27, 29.2, 0.1, 90.0, 90, 1, 0),                    # ok
           (271000002, "14:10", 41.0, 29.0, 102.3, 360.0, 511, 15, -128),             # all sentinels
           (271000003, "14:10", 91.0, 181.0, 5.0, 10.0, 10, 0, 0),                    # lat/lon sentinel
           (271000004, "14:10", 0.0, 0.0, -1.0, 400.0, 400, 99, 500),                 # null island + out of range
           (12345, "14:10", 50.0, 29.2, 5.0, 10.0, 10, 1, 0),                         # outside region + conflict
           (970000001, "14:10", None, None, None, None, None, None, None)]            # all missing
    pos += [(271000001, "15:%02d" % i, 41.27, 29.2, s, 90.0, 90, 0, 0) for i, s in
            enumerate([0.1, 0.2, 0.3, 0.1, 0.2, 0.3, 0.2, 0.1, 0.2, 25.0])]           # last one: unusual_high
    for m, t, *vals in pos:
        con.execute(ins, (vk[m], "2026-10-11T%s:00.000Z" % t, *vals, ts))
    con.execute("INSERT INTO marine_point (latitude, longitude, retrieved_at, source_file) VALUES (41.375, 29.208, ?, 't/x')", (ts,))
    con.execute("INSERT INTO marine_point (latitude, longitude, retrieved_at, source_file) VALUES (40.0, 26.0, ?, 't/x')", (ts,))
    con.execute("INSERT INTO marine_point (latitude, longitude, retrieved_at, source_file) VALUES (39.0, 27.0, ?, 't/x')", (ts,))
    p1, p2, p3 = [k for (k,) in con.execute("SELECT point_key FROM marine_point ORDER BY point_key")]
    h = [.4, .5, .45, None, .5, -1.0, 99.0, .55, .6, .5] + [.8] * 7 + [.6, .5, .55, 3.5, .5] \
        + [1.0, 1.6, 2.4, 3.0, 3.4, 3.0, 2.4, 1.6]
    w_ins = ("INSERT INTO wave_reading (point_key, observed_at, wave_height, wave_direction, wave_period, "
             "wind_wave_height, retrieved_at, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, 't/x')")
    for i, v in enumerate(h):
        d = 400.0 if i == 0 else 180.0
        p = -1.0 if i == 1 else 6.0
        w = 1.0 if i == 2 else (0.3 if v is None or v < 0 or v > 50 else round(v * 0.8, 2))
        con.execute(w_ins, (p1, "2026-10-%02dT%02d:00Z" % (11 + i // 24, i % 24), v, d, p, w, ts))
    for i in range(5):
        con.execute(w_ins, (p2, "2026-10-11T%02d:00Z" % i, None, None, None, None, ts))
    for i in range(12):   # calm sea on the model grid: identical values are expected, not a fault
        con.execute(w_ins, (p3, "2026-10-11T%02d:00Z" % i, 0.04 if i < 10 else 0.06, 180.0, 4.0, 0.03, ts))
    con.close()


def flags(db, sql, params=()):
    con = connect(db)
    rows = con.execute(sql, params).fetchall()
    con.close()
    return rows


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = tmp / "m3.db"
        make_db(db)
        con = connect(db)
        raw0 = table_digest(con)
        con.close()
        quiet = lambda m: None
        rc1 = clean.run(db, out=quiet)
        con = connect(db)
        raw1, c1 = table_digest(con), clean.clean_digest(con)
        con.close()
        report("M2 tables untouched", rc1 == 0 and raw0 == raw1)
        rc2 = clean.run(db, out=quiet)
        con = connect(db)
        c2 = clean.clean_digest(con)
        con.close()
        report("idempotent (run twice)", rc2 == 0 and c1 == c2)

        def one(sql, *p):
            return flags(db, sql, p)[0][0]
        n = lambda t: one("SELECT COUNT(*) FROM " + t)  # test-only helper on fixed table names
        report("same row count as raw", n("vessel_clean") == n("vessel") and n("ais_position_clean") == n("ais_position")
               and n("wave_reading_clean") == n("wave_reading"))

        v = {r[0]: r for r in flags(db, "SELECT v.mmsi, c.mmsi, c.mmsi_flag, c.ship_name, c.ship_name_flag "
                                        "FROM vessel_clean c JOIN vessel v USING (vessel_key)")}
        report("vessel: mmsi and names",
               v[12345][1:3] == (None, "invalid_length") and v[970000001][2] == "not_a_ship"
               and v[271000002][3:] == (None, "missing_not_reported") and v[271000007][3:] == (None, "placeholder_name")
               and v[271000003][3] == "\u0130STANBUL" and v[271000003][4] == "normalised" and v[271000001][4] == "ok", str(v[271000003]))

        a = {r[0]: r[1:] for r in flags(db, "SELECT v.mmsi, c.sog_flag, c.cog_flag, c.true_heading_flag, c.nav_status_flag, "
                                            "c.rate_of_turn_flag, c.latitude_flag, c.longitude_flag, c.row_flag, c.sog, c.latitude "
                                            "FROM ais_position_clean c JOIN vessel v USING (vessel_key) "
                                            "WHERE c.observed_at LIKE '%T14:10%'")}
        report("sentinels become NULL + flag", a[271000002][:5] == ("invalid_sentinel",) * 5 and a[271000002][8] is None, str(a[271000002]))
        report("lat/lon sentinel", a[271000003][5:7] == ("invalid_sentinel", "invalid_sentinel") and a[271000003][8] == 5.0 and a[271000003][9] is None)
        report("out of range + null island", a[271000004][0] == "invalid_out_of_range" and a[271000004][1] == "invalid_out_of_range"
               and a[271000004][4] == "invalid_out_of_range" and a[271000004][5] == "invalid_null_island", str(a[271000004]))
        report("outside region + conflict", a[12345][5] == "invalid_outside_region" and a[12345][7] == "nav_status_conflicts_with_sog")
        report("missing + valid kept", a[970000001][0] == "missing_not_reported" and a[271000001][0] == "ok"
               and a[271000001][8] == 0.1 and a[271000001][9] == 41.27)
        report("unusual_high kept (IQR)", flags(db, "SELECT sog_flag, sog FROM ais_position_clean WHERE sog_raw = 25.0")[0] == ("unusual_high", 25.0))

        hw = [r for r in flags(db, "SELECT wave_height_flag, wave_height FROM wave_reading_clean WHERE point_key = "
                                   "(SELECT MIN(point_key) FROM marine_point) ORDER BY observed_at")]
        f = [x[0] for x in hw]
        report("wave: missing/invalid", f[3] == "missing_not_reported" and f[5] == "invalid_negative" and f[6] == "invalid_out_of_range"
               and hw[3][1] is None and hw[5][1] is None and hw[6][1] is None and f[0] == "ok", str(f[:8]))
        report("wave: stuck run kept", f[10:17] == ["stuck_value"] * 7 and hw[12][1] == 0.8)
        report("wave: spike kept", f[20] == "spike" and hw[20][1] == 3.5, f[20])
        report("wave: unusual_high kept", f[26] == "unusual_high" and hw[26][1] == 3.4, f[26])
        o = flags(db, "SELECT wave_direction_flag, wave_period_flag, wind_wave_height_flag FROM wave_reading_clean "
                      "WHERE point_key = (SELECT MIN(point_key) FROM marine_point) ORDER BY observed_at LIMIT 3")
        report("wave: other columns", o[0][0] == "invalid_out_of_range" and o[1][1] == "invalid_out_of_range"
               and o[2][2] == "inconsistent_exceeds_total", str(o))
        cov = one("SELECT COUNT(*) FROM wave_reading_clean WHERE wave_height_flag = 'missing_no_model_coverage' "
                  "AND point_key = (SELECT point_key FROM marine_point WHERE latitude = 40.0)")
        report("wave: no model coverage", cov == 5)
        calm = one("SELECT COUNT(*) FROM wave_reading_clean WHERE wave_height_flag <> 'ok' AND "
                   "point_key = (SELECT point_key FROM marine_point WHERE latitude = 39.0)")
        report("wave: calm identical run not stuck", calm == 0, "%d flagged" % calm)

        # ---- no imputation anywhere: cleaned value is either the raw value or NULL
        imp = one("SELECT COUNT(*) FROM wave_reading_clean WHERE wave_height IS NOT NULL AND wave_height IS NOT wave_height_raw") \
            + one("SELECT COUNT(*) FROM ais_position_clean WHERE sog IS NOT NULL AND sog IS NOT sog_raw")
        report("no imputation", imp == 0)

        # ---- a threshold moves exactly what it affects, and moving it back restores everything
        rc = clean.run(db, out=quiet, STUCK_RUN=100)
        con = connect(db)
        moved = con.execute("SELECT COUNT(*) FROM wave_reading_clean WHERE wave_height_flag = 'stuck_value'").fetchone()[0]
        con.close()
        rc_b = clean.run(db, out=quiet)
        con = connect(db)
        back = clean.clean_digest(con)
        con.close()
        report("threshold change + restore", rc == 0 and moved == 0 and rc_b == 0 and back == c1)

        # ---- atomic: an injected failure in the SECOND table leaves the first one as it was
        con = connect(db)
        con.execute("CREATE TRIGGER boom BEFORE INSERT ON wave_reading_clean BEGIN SELECT RAISE(ABORT, 'injected'); END")
        con.close()
        rc_f = clean.run(db, out=quiet, IQR_K=0.0)   # would change AIS flags if it were not rolled back
        con = connect(db)
        after = clean.clean_digest(con)
        con.execute("DROP TRIGGER boom")
        con.close()
        report("atomic (injected failure)", rc_f != 0 and after == c1)
        clean.run(db, out=quiet)

        # ---- validate: all PASS/WARN on clean data, and each STOP can fail
        lines = []
        rc_v = validate.run(db, out=lines.append)
        report("validate on good data", rc_v == 0 and not any(l.startswith("STOP") for l in lines),
               [l for l in lines if l.startswith("WARN")][0][:60] if any(l.startswith("WARN") for l in lines) else "")

        def break_and_validate(label, sql, key, fk_off=False):
            d = tmp / "stop.db"
            shutil.copyfile(db, d)
            c = connect(d)
            if fk_off:
                c.execute("PRAGMA foreign_keys = OFF")
            c.execute(sql)
            c.close()
            out_lines = []
            rc = validate.run(d, out=out_lines.append)
            hit = [l for l in out_lines if key in l]
            report("STOP fails: " + label, rc == 1 and bool(hit) and hit[0].startswith("STOP"))
        break_and_validate("row count", "DELETE FROM ais_position_clean WHERE vessel_key = (SELECT MIN(vessel_key) FROM ais_position_clean)",
                           "row count")
        break_and_validate("impossible value", "UPDATE ais_position_clean SET sog = 500 WHERE vessel_key = (SELECT MIN(vessel_key) FROM ais_position_clean)",
                           "impossible")
        break_and_validate("foreign key", "INSERT INTO ais_position (vessel_key, observed_at, retrieved_at, source_file) "
                           "VALUES (9999, '2026-10-11T10:00:00.000Z', '2026-10-11T16:00:00Z', 't/x')", "foreign_key_check", fk_off=True)
        break_and_validate("raw preserved", "UPDATE ais_position_clean SET sog_raw = 1.23 WHERE vessel_key = (SELECT MIN(vessel_key) FROM ais_position_clean)",
                           "_raw columns")

    # ---- static scans of the repository
    srcs = {p.name: p.read_text() for p in (ROOT / "src").glob("*.py")}
    m2 = ("vessel", "marine_point", "ais_position", "wave_reading", "quarantine")
    bad = [(n, m.group(0)) for n, t in srcs.items() if n in ("clean.py", "validate.py") for m in
           re.finditer(r"(?:UPDATE|DELETE FROM|DROP\s+\w+(?:\s+IF EXISTS)?|INSERT INTO)\s+(\w+)", t) if m.group(1) in m2]
    report("no write to an M2 table in clean code", not bad, str(bad))
    fmt = [(n, m.group(0)) for n, t in srcs.items() for m in re.finditer(r"""f["'](SELECT|INSERT|UPDATE|DELETE)""", t)]
    report("no f-string SQL in the repository", not fmt, str(fmt))

    ok = all(RESULTS)
    print("\nALL M3 CHECKS PASSED" if ok else "\nSOME M3 CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
