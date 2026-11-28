#!/usr/bin/env python3
"""M5 monitor: the checks that run OUTSIDE the pipeline. Read-only.

Run from the repository root:  python src/monitor.py [--now 2026-11-20T09:30Z]
Prints one line per check, PASS or ALARM, and exits 1 if any check raises an ALARM. Schedule it separately from
the pipeline (ops/crontab.txt), a few minutes after each run: it still runs when the pipeline's own schedule broke.
"""
import argparse
import os
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

from db import DEFAULT_DB, ROOT, format_run_time, parse_run_time

LOG_DB = ROOT / os.environ.get("PIPELINE_LOG_DB", "data/pipeline_log.db")

# ---- thresholds. The schedule is hourly (ops/crontab.txt); each value says why.
N_STEPS = 4                    # ingest, load, clean, validate: a "fully successful" run has this many rows, all exit code 0
LAST_SUCCESS_MAX_HOURS = 3     # 3 x the schedule interval: two missed runs plus slack for a long ingest
GAP_MAX_HOURS = 2.5            # consecutive runs further apart than this mean at least one missed run (1 h + slack)
GAP_LOOKBACK_DAYS = 7          # one week of history is what the evidence in docs/pipeline.md covers
COMPLETENESS_HOURS = 48        # hours looked at: long enough to see a night missed, short enough to stay current
AIS_MIN_POSITIONS_PER_HOUR = 1 # every hour has a 10-minute AIS window; a vessel-less hour means the window was lost
FEED_MAX_AGE_HOURS = 3         # newest landed AIS / marine file; same reasoning as LAST_SUCCESS_MAX_HOURS
WAVE_LATEST_MAX_AGE_HOURS = 6  # a point's newest wave value may lie this far before now (the model is a forecast: normally in the future)

LAST_SUCCESS_SQL = """
SELECT MAX(run_time) AS last_success,
       ROUND((julianday(:now) - julianday(MAX(run_time))) * 24, 1) AS hours_ago
FROM (SELECT run_id, run_time FROM pipeline_run
      GROUP BY run_id HAVING COUNT(*) = :n_steps AND MAX(exit_code) = 0)
"""
GAPS_SQL = """
WITH runs AS (SELECT DISTINCT run_time FROM pipeline_run WHERE run_time >= :since AND run_time <= :now)
SELECT * FROM (
    SELECT LAG(run_time) OVER (ORDER BY run_time) AS previous_run, run_time,
           ROUND((julianday(run_time) - julianday(LAG(run_time) OVER (ORDER BY run_time))) * 24, 1) AS hours_since
    FROM runs)
WHERE hours_since > :max_gap_hours
"""
WAVE_COMPLETENESS_SQL = """
WITH RECURSIVE periods(p) AS (
    SELECT :first_period
    UNION ALL
    SELECT strftime('%Y-%m-%dT%H:%MZ', p, '+1 hour') FROM periods WHERE p < :last_period)
SELECT p AS period, COUNT(r.point_key) AS points
FROM periods LEFT JOIN wave_reading_clean AS r ON r.observed_at = p AND r.wave_height IS NOT NULL
GROUP BY p HAVING COUNT(r.point_key) < :expected_points
"""
AIS_COMPLETENESS_SQL = """
WITH RECURSIVE periods(p) AS (
    SELECT :first_period
    UNION ALL
    SELECT strftime('%Y-%m-%dT%H:%MZ', p, '+1 hour') FROM periods WHERE p < :last_period)
SELECT p AS period, COUNT(a.vessel_key) AS positions
FROM periods LEFT JOIN ais_position AS a ON substr(a.observed_at, 1, 13) = substr(p, 1, 13)
GROUP BY p HAVING COUNT(a.vessel_key) < :min_positions
"""
WAVE_FRESHNESS_SQL = """
SELECT point_key, MAX(observed_at) AS latest FROM wave_reading_clean
WHERE wave_height IS NOT NULL GROUP BY point_key
HAVING MAX(observed_at) < strftime('%Y-%m-%dT%H:%MZ', :now, :back)
"""


def open_ro(path):
    path = Path(path)
    if not path.exists():
        return None
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def run(db_path=None, log_path=None, now=None, out=print) -> int:
    now = parse_run_time(None) if now is None else now
    now_s = format_run_time(now)
    alarms = []

    def report(name, ok, detail):
        out("%-6s %-34s %s" % ("PASS" if ok else "ALARM", name, detail))
        if not ok:
            alarms.append(name)

    log = open_ro(log_path or LOG_DB)
    db = open_ro(db_path or DEFAULT_DB)
    out("monitor time: %s" % now_s)
    if log is None:
        report("run log exists", False, "%s not found: the pipeline has never run here" % (log_path or LOG_DB))
    if db is None:
        report("database exists", False, "%s not found" % (db_path or DEFAULT_DB))
    try:
        if log is not None:
            row = log.execute(LAST_SUCCESS_SQL, {"now": now_s, "n_steps": N_STEPS}).fetchone()
            ok = row[0] is not None and row[1] <= LAST_SUCCESS_MAX_HOURS
            report("last success", ok, "last fully successful run %s, %s h before now (limit %s h)" % (
                row[0], row[1], LAST_SUCCESS_MAX_HOURS) if row[0] else "no fully successful run in the log")
            since = format_run_time(now - timedelta(days=GAP_LOOKBACK_DAYS))
            gaps = log.execute(GAPS_SQL, {"since": since, "now": now_s, "max_gap_hours": GAP_MAX_HOURS}).fetchall()
            report("gaps between runs", not gaps, "no gap above %s h in the last %d days" % (GAP_MAX_HOURS, GAP_LOOKBACK_DAYS)
                   if not gaps else "%d gap(s) above %s h, e.g. %s -> %s (%s h)" % (len(gaps), GAP_MAX_HOURS, gaps[0][0], gaps[0][1], gaps[0][2]))
            first_run = log.execute("SELECT MIN(run_time) FROM pipeline_run").fetchone()[0]
        else:
            first_run = None
        if db is not None:
            lo = floor_hour(now - timedelta(hours=COMPLETENESS_HOURS))
            if first_run:
                lo = max(lo, floor_hour(parse_run_time(first_run)))
            hour_now = floor_hour(now)
            expected = db.execute("SELECT COUNT(DISTINCT point_key) FROM wave_reading_clean WHERE wave_height IS NOT NULL").fetchone()[0]
            miss = db.execute(WAVE_COMPLETENESS_SQL, {"first_period": format_run_time(lo), "last_period": format_run_time(hour_now),
                                                      "expected_points": expected}).fetchall()
            report("completeness: wave hours", not miss, "all %d points have a value in every hour %s .. %s" % (
                expected, format_run_time(lo), format_run_time(hour_now)) if not miss else
                   "%d hour(s) with fewer than %d points, e.g. %s (%d points)" % (len(miss), expected, miss[0][0], miss[0][1]))
            ais_miss = db.execute(AIS_COMPLETENESS_SQL, {"first_period": format_run_time(lo),
                                                         "last_period": format_run_time(hour_now - timedelta(hours=1)),
                                                         "min_positions": AIS_MIN_POSITIONS_PER_HOUR}).fetchall()
            report("completeness: AIS hours", not ais_miss, "every hour %s .. %s has AIS positions" % (
                format_run_time(lo), format_run_time(hour_now - timedelta(hours=1))) if not ais_miss else
                   "%d hour(s) without AIS positions, lost for good (no history at the source), e.g. %s" % (len(ais_miss), ais_miss[0][0]))
            stale = db.execute(WAVE_FRESHNESS_SQL, {"now": now_s, "back": "-%d hours" % WAVE_LATEST_MAX_AGE_HOURS}).fetchall()
            report("freshness: wave per point", not stale, "every point has a value up to or beyond now - %d h" % WAVE_LATEST_MAX_AGE_HOURS
                   if not stale else "points with an old latest value: %s" % [(k, v) for k, v in stale])
            ages = []
            for label, sql in (("AIS", "SELECT MAX(retrieved_at) FROM ais_position"), ("marine", "SELECT MAX(retrieved_at) FROM wave_reading")):
                last = db.execute(sql).fetchone()[0]
                ages.append((label, last, None if last is None else (now - parse_run_time(last[:16] + "Z")).total_seconds() / 3600.0))
            bad = [a for a in ages if a[2] is None or a[2] > FEED_MAX_AGE_HOURS]
            report("freshness: newest landed file", not bad, ", ".join("%s %s (%.1f h)" % (a[0], a[1][:16] + "Z", a[2]) for a in ages)
                   if not bad else "too old (limit %d h): %s" % (FEED_MAX_AGE_HOURS, [(a[0], a[1], a[2]) for a in bad]))
    except sqlite3.Error as exc:
        report("monitor could read its inputs", False, repr(exc))
    finally:
        for con in (log, db):
            if con is not None:
                con.close()
    out("%d ALARM(s)" % len(alarms) if alarms else "all checks PASS")
    return 1 if alarms else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--now", help="the monitor's clock, e.g. 2026-11-20T09:30Z (default: now)")
    ap.add_argument("--db", help="main database (default data/maritime.db)")
    ap.add_argument("--log", help="run log (default data/pipeline_log.db)")
    args = ap.parse_args()
    return run(args.db, args.log, parse_run_time(args.now))


if __name__ == "__main__":
    sys.exit(main())
