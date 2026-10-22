#!/usr/bin/env python3
"""M2 checks: prove the database refuses what it should. Runs on a TEMPORARY copy.

Run from the repository root:  python src/checks.py
"""
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import load
from db import DEFAULT_DB, connect, table_counts, table_digest

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append(ok)
    print("%-22s %s%s" % (name + ":", "PASS" if ok else "FAIL", ("  " + detail) if detail else ""))


def rejected(con, setup, sql, params):
    """True if `sql` raises IntegrityError. Always rolled back."""
    con.execute("BEGIN")
    try:
        for s, p in setup:
            con.execute(s, p)
        con.execute(sql, params)
        return False
    except sqlite3.IntegrityError:
        return True
    finally:
        con.execute("ROLLBACK")


ADD_VESSEL = ("INSERT INTO vessel (mmsi, ship_name, retrieved_at, source_file) VALUES (?, ?, ?, ?)",
              (999000001, "CHECK VESSEL", "2026-01-01T00:00:00Z", "check/x"))
INS_POS = ("INSERT INTO ais_position (vessel_key, observed_at, sog, retrieved_at, source_file) "
           "VALUES ((SELECT vessel_key FROM vessel WHERE mmsi = ?), ?, ?, ?, ?)")


def quiet(_msg):
    pass


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = tmp / "check.db"
        if DEFAULT_DB.exists():
            shutil.copyfile(DEFAULT_DB, db)
        elif load.run(db, out=quiet) != 0:
            print("Could not build a database from data/raw - run ingest first.")
            return 1
        con = connect(db)
        con.execute("SELECT 1 FROM vessel LIMIT 1")  # schema present

        # 1. duplicate natural key
        report("duplicate natural key", rejected(
            con, [ADD_VESSEL],
            "INSERT INTO vessel (mmsi, retrieved_at, source_file) VALUES (?, ?, ?)",
            (999000001, "2026-01-01T00:00:00Z", "check/y")))

        # 2. orphan event
        report("orphan event", rejected(
            con, [], INS_POS.replace("(SELECT vessel_key FROM vessel WHERE mmsi = ?)", "?"),
            (-12345, "2026-01-01T00:00:00.000Z", 1.0, "2026-01-01T00:00:00Z", "check/x")))

        # 3. NULL key
        report("NULL key", rejected(
            con, [ADD_VESSEL], INS_POS,
            (999000001, None, 1.0, "2026-01-01T00:00:00Z", "check/x")))

        # 4. wrong type
        report("wrong type", rejected(
            con, [ADD_VESSEL], INS_POS,
            (999000001, "2026-01-01T00:00:00.000Z", "n/a", "2026-01-01T00:00:00Z", "check/x")))
        con.close()

        # 5. idempotence: from-scratch DB, load twice, compare counts AND contents
        db2 = tmp / "idem.db"
        rc1 = load.run(db2, out=quiet)
        c1 = connect(db2)
        counts1, dig1 = table_counts(c1), table_digest(c1)
        c1.close()
        rc2 = load.run(db2, out=quiet)
        c2 = connect(db2)
        counts2, dig2 = table_counts(c2), table_digest(c2)
        c2.close()
        report("idempotence", rc1 == 0 and rc2 == 0 and counts1 == counts2 and dig1 == dig2,
               "counts %s" % counts1 if counts1 == counts2 else "%s -> %s" % (counts1, counts2))

        # 6. atomicity: good new AIS file (writes rows) + corrupt marine file (fails afterwards)
        raw = tmp / "raw"
        (raw / "ais").mkdir(parents=True)
        (raw / "marine").mkdir()
        good = {"MessageType": "PositionReport",
                "MetaData": {"MMSI": 999000002, "ShipName": "ATOMIC TEST", "latitude": 37.0,
                             "longitude": 26.0, "time_utc": "2099-12-31 23:59:59.000000000 +0000 UTC"},
                "Message": {"PositionReport": {"Latitude": 37.0, "Longitude": 26.0, "Sog": 5.0,
                                               "Cog": 90.0, "TrueHeading": 90, "NavigationalStatus": 0,
                                               "RateOfTurn": 0}}}
        (raw / "ais" / "20991231T000000Z.jsonl").write_text(json.dumps(good) + "\n")
        (raw / "marine" / "20991231T000000Z.json").write_text("{ this is not json")

        ctrl = tmp / "control.db"
        shutil.copyfile(db2, ctrl)
        shutil.rmtree(raw / "marine")  # control: the AIS file alone must change the database
        load.run(ctrl, raw, out=quiet)
        cc = connect(ctrl)
        control_changed = table_counts(cc) != counts2
        cc.close()
        (raw / "marine").mkdir()
        (raw / "marine" / "20991231T000000Z.json").write_text("{ this is not json")

        con = connect(db2)
        before_c, before_d = table_counts(con), table_digest(con)
        con.close()
        rc = load.run(db2, raw, out=quiet)
        con = connect(db2)
        after_c, after_d = table_counts(con), table_digest(con)
        con.close()
        report("atomicity", rc != 0 and control_changed and before_c == after_c and before_d == after_d,
               "" if before_c == after_c else "%s -> %s" % (before_c, after_c))

    ok = all(RESULTS)
    print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
