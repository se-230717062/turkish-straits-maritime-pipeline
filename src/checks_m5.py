#!/usr/bin/env python3
"""M5 checks: the runner, the lock, the failure drills, healing, the guarded upsert, validate and monitor.

Everything runs on temporary paths (DB_PATH, RAW_DATA_DIR, PIPELINE_LOG_DB, PIPELINE_LOCK point into a temp
directory); it never touches data/. Run from the repository root:  python src/checks_m5.py
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clean
import ingest
import load
import monitor
import run_pipeline
import validate
from db import ROOT, connect, format_run_time, parse_run_time, table_digest

RESULTS = []
RUNNER = [sys.executable, str(ROOT / "src" / "run_pipeline.py")]
UTC = timezone.utc


def report(name, ok, detail=""):
    RESULTS.append(ok)
    print("%-44s %s%s" % (name + ":", "PASS" if ok else "FAIL", ("  " + str(detail)) if detail else ""))


# ---------------------------------------------------------------- synthetic raw data
POINTS = [(38.458, 25.875), (40.042, 26.125), (40.458, 26.708)]


def stamp(dt):
    return dt.strftime("%Y%m%dT%H%M%SZ")


def make_raw(raw, run_times, revise=True, k_start=0):
    """One AIS file and one marine file per run, like the real ones. Newer marine files revise the older values."""
    (raw / "ais").mkdir(parents=True, exist_ok=True)
    (raw / "marine").mkdir(parents=True, exist_ok=True)
    for k, rt in enumerate(run_times, start=k_start):
        lines = []
        for v in range(6):
            t = rt + timedelta(minutes=1 + v)
            lines.append(json.dumps({"MessageType": "PositionReport", "MetaData": {
                "MMSI": 271000000 + v, "ShipName": "SHIP %d" % v, "latitude": 40.0 + v / 10, "longitude": 27.0,
                "time_utc": t.strftime("%Y-%m-%d %H:%M:%S.000000000 +0000 UTC")},
                "Message": {"PositionReport": {"Latitude": 40.0 + v / 10, "Longitude": 27.0, "Sog": float((v + k) % 5),
                                               "Cog": 90.0, "TrueHeading": 90, "NavigationalStatus": 0, "RateOfTurn": 0}}}))
        (raw / "ais" / (stamp(rt) + ".jsonl")).write_text("\n".join(lines) + "\n")
        start = (rt - timedelta(days=1)).replace(hour=0, minute=0)
        times = [start + timedelta(hours=h) for h in range(72)]
        body = []
        for pi, (la, lo) in enumerate(POINTS):
            vals = [round(0.1 + 0.02 * ((h + pi) % 7) + (0.02 * k if revise else 0), 2) for h in range(72)]
            body.append({"latitude": la, "longitude": lo, "utc_offset_seconds": 0,
                         "hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in times], "wave_height": vals,
                                    "wave_direction": [180.0] * 72, "wave_period": [4.0] * 72, "wind_wave_height": [0.05] * 72}})
        (raw / "marine" / (stamp(rt) + ".json")).write_text(json.dumps(body))


def env_for(tmp, **extra):
    env = dict(os.environ)
    env.update(DB_PATH=str(tmp / "m.db"), RAW_DATA_DIR=str(tmp / "raw"), PIPELINE_LOG_DB=str(tmp / "log.db"),
               PIPELINE_LOCK=str(tmp / "lock"))
    env.pop("PIPELINE_DRILL", None)
    env.pop("AISSTREAM_API_KEY", None)
    env.update(extra)
    return env


def runner(tmp, *args, **extra_env):
    return subprocess.run(RUNNER + list(args), cwd=ROOT, env=env_for(tmp, **extra_env), capture_output=True, text=True)


def log_rows(tmp):
    con = sqlite3.connect(tmp / "log.db")
    rows = con.execute("SELECT run_id, run_time, step, exit_code FROM pipeline_run ORDER BY started_at, rowid").fetchall()
    con.close()
    return rows


def digests(tmp):
    con = connect(tmp / "m.db")
    try:
        return table_digest(con), clean.clean_digest(con)
    finally:
        con.close()


def natural_dump(path):
    """Content by natural keys (surrogate keys depend on the load order)."""
    con = connect(path)
    try:
        return (con.execute("SELECT v.mmsi, p.observed_at, p.sog, p.retrieved_at FROM ais_position p JOIN vessel v USING (vessel_key) ORDER BY 1, 2").fetchall(),
                con.execute("SELECT m.latitude, m.longitude, w.observed_at, w.wave_height, w.retrieved_at FROM wave_reading w JOIN marine_point m USING (point_key) ORDER BY 1, 2, 3").fetchall())
    finally:
        con.close()


def main() -> int:
    t0 = datetime(2026, 11, 20, 8, 7, tzinfo=UTC)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_raw(tmp / "raw", [t0, t0 + timedelta(hours=1), t0 + timedelta(hours=2)])

        # 1. builds from scratch without the network
        r = runner(tmp, "--skip-ingest", "--run-time", "2026-11-20T10:30Z")
        rows = log_rows(tmp)
        report("--skip-ingest builds from scratch", r.returncode == 0 and [x[2] for x in rows] == ["src/load.py", "src/clean.py", "src/validate.py"]
               and all(x[3] == 0 for x in rows), "exit %d, steps logged: %s" % (r.returncode, [Path(x[2]).stem for x in rows]))
        report("closing line says OK", r.stdout.strip().splitlines()[-1].startswith("PIPELINE OK"), r.stdout.strip().splitlines()[-1][:60])
        d1 = digests(tmp)

        # 1b. a run log started by an older runner (no detail column) keeps its rows and keeps working
        old_log = tmp / "oldlog.db"
        c = sqlite3.connect(old_log)
        c.execute("CREATE TABLE pipeline_run (run_id TEXT NOT NULL, run_time TEXT NOT NULL, step TEXT NOT NULL, started_at TEXT NOT NULL, "
                  "finished_at TEXT NOT NULL, exit_code INTEGER NOT NULL, PRIMARY KEY (run_id, step)) STRICT")
        c.execute("INSERT INTO pipeline_run VALUES ('old00001', '2026-11-19T08:07Z', 'src/load.py', 'a', 'b', 0)")
        c.commit(); c.close()
        r1b = runner(tmp, "--skip-ingest", "--run-time", "2026-11-20T10:31Z", PIPELINE_LOG_DB=str(old_log))
        c = sqlite3.connect(old_log)
        cols = [x[1] for x in c.execute("PRAGMA table_info(pipeline_run)")]
        n_old = c.execute("SELECT COUNT(*) FROM pipeline_run").fetchone()[0]
        c.close()
        report("old run log is migrated, old rows kept", r1b.returncode == 0 and "detail" in cols and n_old == 4, "rows %d" % n_old)

        # 2. reproducible: same run time, same raw data, identical tables
        r2 = runner(tmp, "--skip-ingest", "--run-time", "2026-11-20T10:30Z")
        report("two runs, same run time: identical tables", r2.returncode == 0 and digests(tmp) == d1)

        # 3. a failing step stops the run, exit code is the step's code, nothing after it ran, database unchanged by that run
        before_rows = len(log_rows(tmp))
        con = connect(tmp / "m.db")
        con.execute("CREATE TRIGGER boom BEFORE INSERT ON wave_reading_clean BEGIN SELECT RAISE(ABORT, 'injected'); END")
        con.close()
        r3 = runner(tmp, "--skip-ingest", "--run-time", "2026-11-20T10:30Z")
        new = log_rows(tmp)[before_rows:]
        report("injected db error in clean: stops, code logged", r3.returncode == 1 and [(Path(x[2]).stem, x[3]) for x in new] == [("load", 0), ("clean", 1)]
               and "FAILED" in r3.stdout and digests(tmp) == d1, "exit %d, logged %s" % (r3.returncode, [(Path(x[2]).stem, x[3]) for x in new]))
        con = connect(tmp / "m.db")
        con.execute("DROP TRIGGER boom")
        con.close()

        # 4. a crashing step (drill)
        before_rows = len(log_rows(tmp))
        r4 = subprocess.run(RUNNER + ["--skip-ingest", "--run-time", "2026-11-20T10:30Z"], cwd=ROOT,
                            env=env_for(tmp, PIPELINE_DRILL="load=fail"), capture_output=True, text=True)
        new = log_rows(tmp)[before_rows:]
        report("drill load=fail: one row, nothing after", r4.returncode == 1 and [(Path(x[2]).stem, x[3]) for x in new] == [("load", 1)])

        # 5. a hanging step times out with exit code 124
        before_rows = len(log_rows(tmp))
        r5 = subprocess.run(RUNNER + ["--skip-ingest", "--run-time", "2026-11-20T10:30Z", "--timeout", "4"], cwd=ROOT,
                            env=env_for(tmp, PIPELINE_DRILL="load=hang"), capture_output=True, text=True)
        new = log_rows(tmp)[before_rows:]
        report("drill load=hang: exit code 124, logged", r5.returncode == 124 and [(Path(x[2]).stem, x[3]) for x in new] == [("load", 124)])

        # 6. one at a time: a live lock -> 75, nothing run; a stale lock is taken over
        before_rows = len(log_rows(tmp))
        first = subprocess.Popen(RUNNER + ["--skip-ingest", "--run-time", "2026-11-20T10:30Z", "--timeout", "6"], cwd=ROOT,
                                 env=env_for(tmp, PIPELINE_DRILL="load=hang"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(50):
            if (tmp / "lock").exists():
                break
            time.sleep(0.1)
        second = runner(tmp, "--skip-ingest", "--run-time", "2026-11-20T10:30Z")
        report("two runs at once: second exits 75, runs nothing", second.returncode == 75 and "NOT RUN" in second.stdout
               and len(log_rows(tmp)) == before_rows)
        first.communicate(timeout=60)
        (tmp / "lock").write_text("99999999\n")
        r6 = runner(tmp, "--skip-ingest", "--run-time", "2026-11-20T10:30Z")
        report("stale lock (dead pid) is taken over", r6.returncode == 0 and not (tmp / "lock").exists())

        # 7. credential failure: ingest exits non-zero, the runner stops there, one row
        before_rows = len(log_rows(tmp))
        r7 = runner(tmp, "--run-time", "2026-11-20T11:07Z")
        new = log_rows(tmp)[before_rows:]
        report("missing API key: ingest fails, one row", r7.returncode == 1 and [(Path(x[2]).stem, x[3]) for x in new] == [("ingest", 1)],
               "exit %d, logged %s" % (r7.returncode, [(Path(x[2]).stem, x[3]) for x in new]))

        # 8. guarded upsert: an old file loaded AFTER a newer one cannot undo it
        a, b = tmp / "rawA", tmp / "rawB"
        times = [t0, t0 + timedelta(hours=1), t0 + timedelta(hours=2)]
        make_raw(a, times[1:], k_start=1); make_raw(b, times[:1])        # A: the two newer runs, B: the oldest run (same hours, older values)
        make_raw(tmp / "rawAll", times)
        db_late, db_ref = tmp / "late.db", tmp / "ref.db"
        load.run(db_late, a, out=lambda m: None); load.run(db_late, b, out=lambda m: None)     # newest first, then the old file
        load.run(db_ref, tmp / "rawAll", out=lambda m: None)                                   # all files, chronological
        la, ra = natural_dump(db_late), natural_dump(db_ref)
        overlap = [w for w in la[1] if w[2] >= "2026-11-20T00:00Z" and w[2] < "2026-11-21T00:00Z"]
        ref_overlap = [w for w in ra[1] if w[2] >= "2026-11-20T00:00Z" and w[2] < "2026-11-21T00:00Z"]
        report("old file loaded after a newer one: newest wins", overlap == ref_overlap and len(overlap) > 0,
               "%d overlapping readings identical" % len(overlap))
        report("any load order gives the same content", la == ra)

        # 9. healing window
        rt = datetime(2026, 11, 23, 9, 7, tzinfo=UTC)
        w_hour = ingest.plan_marine_window(rt, rt - timedelta(hours=1))
        w_gap = ingest.plan_marine_window(rt, rt - timedelta(hours=73))
        w_lost = ingest.plan_marine_window(rt, rt - timedelta(days=100))
        w_first = ingest.plan_marine_window(rt, None)
        report("healing window: normal, gap, beyond memory", w_hour["past_days"] == 1 and w_gap["past_days"] == 4 and not w_gap["lost"]
               and w_lost["lost"] and w_lost["past_days"] == 92 and w_first["past_days"] == 2, "%d, %d, %d d" % (w_hour["past_days"], w_gap["past_days"], w_lost["past_days"]))

        # 10. validate: freshness from the run time and volume
        out_now, out_late = [], []
        validate.run(tmp / "m.db", out=out_now.append, run_time=parse_run_time("2026-11-20T10:30Z"))
        validate.run(tmp / "m.db", out=out_late.append, run_time=parse_run_time("2026-12-05T10:30Z"))
        validate.run(tmp / "m.db", out=(reb := []).append, run_time=parse_run_time("2026-12-05T10:30Z"), rebuild=True)
        fresh_ok = [l for l in out_now if "fresh: AIS" in l][0].startswith("PASS")
        fresh_bad = [l for l in out_late if "fresh: AIS" in l][0].startswith("WARN")
        fresh_info = [l for l in reb if "fresh: AIS" in l][0].startswith("INFO")
        report("validate: freshness from the run time", fresh_ok and fresh_bad and fresh_info)
        report("validate: no permanent WARN on a healthy run", not any(l.startswith("WARN") for l in out_now), [l[:70] for l in out_now if l.startswith("WARN")])

        # 11. monitor: PASS when healthy, and every check can ALARM
        mon = tmp / "mon"; mon.mkdir()
        hourly = [t0 + timedelta(hours=h) for h in range(6)]
        make_raw(mon / "raw", hourly)
        load.run(mon / "m.db", mon / "raw", out=lambda m: None); clean.run(mon / "m.db", out=lambda m: None)

        def make_log(path, runs, fail_step=None, failing_runs=0):
            con = sqlite3.connect(path); con.execute(run_pipeline.CREATE_LOG)
            for i, rt_ in enumerate(runs):
                for s in run_pipeline.STEPS:
                    con.execute(run_pipeline.INSERT_LOG, ("r%d" % i, format_run_time(rt_), s, "x", "x", 1 if s == fail_step and i >= len(runs) - failing_runs else 0, ""))
            con.commit(); con.close()
        make_log(mon / "log.db", hourly)
        now_ok = hourly[-1] + timedelta(minutes=13)

        def mon_run(log=mon / "log.db", db=mon / "m.db", now=now_ok):
            lines = []
            rc = monitor.run(db, log, now, out=lines.append)
            return rc, lines
        rc, lines = mon_run()
        report("monitor: healthy system passes", rc == 0, [l[:60] for l in lines if "ALARM" in l])
        rc, lines = mon_run(now=now_ok + timedelta(hours=5))
        report("monitor ALARM: last success too old", rc == 1 and any(l.startswith("ALARM  last success") for l in lines))
        make_log(mon / "gap.db", hourly[:2] + hourly[4:])
        rc, lines = mon_run(log=mon / "gap.db")
        report("monitor ALARM: gap between runs", rc == 1 and any(l.startswith("ALARM  gaps") for l in lines))
        make_log(mon / "fail.db", hourly, fail_step="src/clean.py", failing_runs=3)   # the last three runs fail in clean
        rc, lines = mon_run(log=mon / "fail.db")
        report("monitor ALARM: newest run failed", rc == 1 and any(l.startswith("ALARM  last success") for l in lines),
               [l[:90] for l in lines if l.startswith("ALARM")])
        con = sqlite3.connect(mon / "m.db"); con.execute("DELETE FROM wave_reading_clean WHERE observed_at = '2026-11-20T10:00Z'")
        con.execute("DELETE FROM ais_position WHERE substr(observed_at, 1, 13) = '2026-11-20T11'"); con.commit(); con.close()
        rc, lines = mon_run()
        report("monitor ALARM: wave completeness hole", rc == 1 and any(l.startswith("ALARM  completeness: wave") for l in lines))
        report("monitor ALARM: AIS completeness hole", any(l.startswith("ALARM  completeness: AIS") for l in lines))
        rc, lines = mon_run(now=now_ok + timedelta(days=3))
        report("monitor ALARM: freshness (point, feed)", any(l.startswith("ALARM  freshness: wave") for l in lines)
               and any(l.startswith("ALARM  freshness: newest") for l in lines))
        rc, lines = mon_run(log=mon / "missing.db")
        report("monitor ALARM: no run log at all", rc == 1 and any("run log exists" in l for l in lines))

    # ---- static scans
    srcs = {p.name: p.read_text() for p in (ROOT / "src").glob("*.py")}
    fmt = [(n, m.group(0)) for n, t in srcs.items() for m in re.finditer(r"""f["'](SELECT|INSERT|UPDATE|DELETE)""", t)]
    report("no f-string SQL in the repository", not fmt, str(fmt))
    now_users = sorted(n for n, t in srcs.items() if "datetime.now(" in t and n != "checks_m5.py")
    report("datetime.now() only in db.py and run_pipeline.py", now_users == ["db.py", "run_pipeline.py"], now_users)

    ok = all(RESULTS)
    print("\nALL M5 CHECKS PASSED" if ok else "\nSOME M5 CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
