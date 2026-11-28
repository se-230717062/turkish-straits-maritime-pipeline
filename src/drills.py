#!/usr/bin/env python3
"""M5 failure drills (section 6), run in a throw-away sandbox. Prints the evidence for docs/pipeline.md.

Run from the repository root:  python src/drills.py [--synthetic] [--network]
The sandbox is a temporary directory: a copy of data/raw (or synthetic raw files with --synthetic), its own
database, run log and lock. Your real database, run log and raw data are never touched.
The sixth drill (missed runs) needs the real schedule; see docs/pipeline.md.
--network also runs the credential drill with a WRONG key against the real AISStream (needs a connection).
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import checks_m5 as K
import clean
import load
from db import ROOT, connect, table_digest

RUN_TIME = "2026-11-20T10:30Z"


def show(title, cmd_env, proc, tmp):
    rows = K.log_rows(tmp) if (tmp / "log.db").exists() else []
    print("\n### %s" % title)
    print("command:      %s" % cmd_env)
    print("exit code:    %d" % proc.returncode)
    print("closing line: %s" % ([ln for ln in proc.stdout.strip().splitlines() if ln.startswith("PIPELINE")] or ["(none)"])[-1])
    return rows


def last_rows(tmp, before):
    return [(Path(r[2]).stem, r[3]) for r in K.log_rows(tmp)[before:]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--synthetic", action="store_true", help="use synthetic raw files instead of data/raw")
    ap.add_argument("--network", action="store_true", help="also run the wrong-API-key drill against AISStream")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        real_raw = ROOT / os.environ.get("RAW_DATA_DIR", "data/raw")
        if args.synthetic or not real_raw.is_dir():
            K.make_raw(tmp / "raw", [datetime(2026, 11, 20, 8, 7, tzinfo=timezone.utc) + timedelta(hours=h) for h in range(3)])
            print("(synthetic raw files)")
        else:
            shutil.copytree(real_raw, tmp / "raw")
            print("(copy of %s)" % real_raw)
        good = K.runner(tmp, "--skip-ingest", "--run-time", RUN_TIME)
        print("sandbox build: exit %d" % good.returncode)
        built = (table_digest(connect(tmp / "m.db")), )

        n = len(K.log_rows(tmp))
        p = K.runner(tmp, "--run-time", RUN_TIME)                                   # no API key in the environment
        show("Drill 1 - credential failure (API key missing)", "run_pipeline.py --run-time %s   (AISSTREAM_API_KEY unset)" % RUN_TIME, p, tmp)
        print("run log rows: %s   (expected: ingest 1, nothing after)" % last_rows(tmp, n))
        if args.network:
            n = len(K.log_rows(tmp))
            p = subprocess.run(K.RUNNER + ["--run-time", RUN_TIME], cwd=ROOT, env=K.env_for(tmp, AISSTREAM_API_KEY="wrong-key"), capture_output=True, text=True)
            show("Drill 1b - credential failure (WRONG key against AISStream)", "AISSTREAM_API_KEY=wrong-key run_pipeline.py", p, tmp)
            print("run log rows: %s" % last_rows(tmp, n))

        con = connect(tmp / "m.db")
        con.execute("CREATE TRIGGER drill_boom BEFORE INSERT ON wave_reading_clean BEGIN SELECT RAISE(ABORT, 'injected'); END")
        con.close()
        before = (table_digest(connect(tmp / "m.db")), clean.clean_digest(connect(tmp / "m.db")))
        n = len(K.log_rows(tmp))
        p = K.runner(tmp, "--skip-ingest", "--run-time", RUN_TIME)
        show("Drill 2 - a step fails (injected database error in clean.py)", "run_pipeline.py --skip-ingest   (trigger on wave_reading_clean)", p, tmp)
        print("run log rows: %s   (expected: load 0, clean 1, no validate)" % last_rows(tmp, n))
        after = (table_digest(connect(tmp / "m.db")), clean.clean_digest(connect(tmp / "m.db")))
        print("database unchanged by the failed run: %s" % (before == after))
        con = connect(tmp / "m.db")
        con.execute("DROP TRIGGER drill_boom")
        con.close()

        n = len(K.log_rows(tmp))
        p = subprocess.run(K.RUNNER + ["--skip-ingest", "--run-time", RUN_TIME, "--timeout", "4"], cwd=ROOT,
                           env=K.env_for(tmp, PIPELINE_DRILL="clean=hang"), capture_output=True, text=True)
        show("Drill 3 - a step hangs (PIPELINE_DRILL=clean=hang, --timeout 4)", "run_pipeline.py --skip-ingest --timeout 4", p, tmp)
        print("run log rows: %s   (expected: load 0, clean 124)" % last_rows(tmp, n))

        n = len(K.log_rows(tmp))
        first = subprocess.Popen(K.RUNNER + ["--skip-ingest", "--run-time", RUN_TIME, "--timeout", "8"], cwd=ROOT,
                                 env=K.env_for(tmp, PIPELINE_DRILL="load=hang"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(50):
            if (tmp / "lock").exists():
                break
            time.sleep(0.1)
        p = K.runner(tmp, "--skip-ingest", "--run-time", RUN_TIME)
        show("Drill 4 - two runs at once", "second run_pipeline.py while the first one hangs", p, tmp)
        print("message:      %s" % p.stdout.strip().splitlines()[-1])
        first.communicate(timeout=60)
        print("run log rows added by the second run: %d   (expected: 0)" % (len([r for r in K.log_rows(tmp)[n:] if r[0] != K.log_rows(tmp)[n][0]]) if len(K.log_rows(tmp)) > n else 0))

        marine = load.list_raw(tmp / "raw" / "marine", "json")
        if len(marine) >= 2:
            old, new = marine[0][1], marine[-1][1]
            db2, only_old = tmp / "late.db", tmp / "only_old"
            (only_old / "marine").mkdir(parents=True)
            shutil.copy(old, only_old / "marine" / old.name)
            only_new = tmp / "only_new"
            (only_new / "marine").mkdir(parents=True)
            shutil.copy(new, only_new / "marine" / new.name)
            load.run(db2, only_new, out=lambda m: None)
            c = sqlite3.connect(db2)
            key = c.execute("SELECT w.point_key, w.observed_at FROM wave_reading w WHERE w.wave_height IS NOT NULL ORDER BY w.observed_at LIMIT 1 OFFSET 20").fetchone()
            val_before = c.execute("SELECT wave_height, retrieved_at FROM wave_reading WHERE point_key = ? AND observed_at = ?", key).fetchone()
            c.close()
            load.run(db2, only_old, out=lambda m: None)                  # the OLD file arrives after the newer one
            c = sqlite3.connect(db2)
            val_after = c.execute("SELECT wave_height, retrieved_at FROM wave_reading WHERE point_key = ? AND observed_at = ?", key).fetchone()
            c.close()
            print("\n### Drill 5 - an old file re-loaded after a newer one")
            print("reading (point %s, %s): after the newest file %s; after re-loading %s: %s" % (
                key[0], key[1], val_before, old.name, val_after))
            print("the newer value stayed: %s" % (val_before == val_after))
        else:
            print("\n### Drill 5 skipped: needs at least two marine files")
        print("\n### Drill 6 - missed runs: needs the real schedule, see docs/pipeline.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
