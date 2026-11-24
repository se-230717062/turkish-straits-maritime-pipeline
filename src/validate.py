#!/usr/bin/env python3
"""M3/M5 validation: expectations on the cleaned layer, as code. PASS / INFO / WARN / STOP per check.

Run from the repository root:  python src/validate.py [--db PATH] [--run-time 2026-11-20T08:10Z]
Exit code 1 if any STOP fails. WARNs are printed every time but do not stop the pipeline.
Every "how old" and "this run" decision is measured from the pipeline's clock (db.run_time()),
never from datetime.now() and never from MAX(observed_at).
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from statistics import median

import clean_config as C
from clean import name_key
from db import RUN_TIME_FMT, connect, run_time

# ---- M5 thresholds. Each one says why it has this value.
# The pipeline runs hourly and a few failed runs in a row do happen (laptop asleep, AIS reconnects).
# Six hours with nothing new means the feed has really stopped; monitor.py alarms much earlier on missed runs.
FEED_MAX_AGE_HOURS = 6
# A run's raw files are stamped within minutes after the run time. One hour never reaches the next run.
RUN_WINDOW_MINUTES = 60
# "Typical run" = the median of the files of the last 24 runs (a day); fewer than 3 files is no baseline yet.
VOLUME_HISTORY_FILES = 24
MIN_BASELINE_FILES = 3
# The assignment calls "ten times the usual" suspicious.
VOLUME_FACTOR = 10
# Wave gaps older than this are history we cannot repair; checking all of it would WARN on every run forever.
RECENT_DAYS = 3

PAIRS = (("vessel_clean", "vessel", "SELECT COUNT(*) FROM vessel_clean", "SELECT COUNT(*) FROM vessel"),
         ("ais_position_clean", "ais_position", "SELECT COUNT(*) FROM ais_position_clean", "SELECT COUNT(*) FROM ais_position"),
         ("wave_reading_clean", "wave_reading", "SELECT COUNT(*) FROM wave_reading_clean", "SELECT COUNT(*) FROM wave_reading"))

IMPOSSIBLE_AIS = (
    "SELECT COUNT(*) FROM ais_position_clean WHERE "
    "(sog IS NOT NULL AND (sog < 0 OR sog > ?)) OR (cog IS NOT NULL AND (cog < 0 OR cog > ?)) "
    "OR (true_heading IS NOT NULL AND (true_heading < 0 OR true_heading > ?)) "
    "OR (nav_status IS NOT NULL AND (nav_status < 0 OR nav_status > ?)) "
    "OR (rate_of_turn IS NOT NULL AND (rate_of_turn < ? OR rate_of_turn > ?)) "
    "OR (latitude IS NOT NULL AND (latitude < ? OR latitude > ?)) "
    "OR (longitude IS NOT NULL AND (longitude < ? OR longitude > ?))")
IMPOSSIBLE_WAVE = (
    "SELECT COUNT(*) FROM wave_reading_clean WHERE "
    "(wave_height IS NOT NULL AND (wave_height < 0 OR wave_height > ?)) "
    "OR (wave_direction IS NOT NULL AND (wave_direction < 0 OR wave_direction > ?)) "
    "OR (wave_period IS NOT NULL AND (wave_period < 0 OR wave_period > ?)) "
    "OR (wind_wave_height IS NOT NULL AND wind_wave_height < 0)")
KEPT_INVALID = (
    "SELECT COUNT(*) FROM ais_position_clean WHERE "
    "(sog IS NOT NULL AND sog_flag <> 'ok' AND sog_flag <> 'unusual_high') OR (cog IS NOT NULL AND cog_flag <> 'ok') "
    "OR (true_heading IS NOT NULL AND true_heading_flag <> 'ok') OR (nav_status IS NOT NULL AND nav_status_flag <> 'ok') "
    "OR (rate_of_turn IS NOT NULL AND rate_of_turn_flag <> 'ok') OR (latitude IS NOT NULL AND latitude_flag <> 'ok') "
    "OR (longitude IS NOT NULL AND longitude_flag <> 'ok')")
RAW_MISMATCH = (
    "SELECT (SELECT COUNT(*) FROM ais_position_clean c JOIN ais_position r "
    "ON r.vessel_key = c.vessel_key AND r.observed_at = c.observed_at WHERE "
    "c.latitude_raw IS NOT r.latitude OR c.longitude_raw IS NOT r.longitude OR c.sog_raw IS NOT r.sog "
    "OR c.cog_raw IS NOT r.cog OR c.true_heading_raw IS NOT r.true_heading OR c.nav_status_raw IS NOT r.nav_status "
    "OR c.rate_of_turn_raw IS NOT r.rate_of_turn) "
    "+ (SELECT COUNT(*) FROM wave_reading_clean c JOIN wave_reading r "
    "ON r.point_key = c.point_key AND r.observed_at = c.observed_at WHERE "
    "c.wave_height_raw IS NOT r.wave_height OR c.wave_direction_raw IS NOT r.wave_direction "
    "OR c.wave_period_raw IS NOT r.wave_period OR c.wind_wave_height_raw IS NOT r.wind_wave_height) "
    "+ (SELECT COUNT(*) FROM vessel_clean c JOIN vessel r ON r.vessel_key = c.vessel_key WHERE "
    "c.mmsi_raw IS NOT r.mmsi OR c.ship_name_raw IS NOT r.ship_name)")

# ---- M5 queries (retrieved_at = when a file was fetched; it is what shows that a feed is alive.
# observed_at is not usable for this: the weather feed also delivers hours that are still in the future.)
LAST_AIS_RETRIEVAL = "SELECT MAX(retrieved_at) FROM ais_position"
HOURS_BEFORE = "SELECT (julianday(?) - julianday(?)) * 24.0"
STALE_MARINE_POINTS = (
    "SELECT p.point_key, MAX(w.retrieved_at) FROM marine_point p "
    "LEFT JOIN wave_reading w ON w.point_key = p.point_key GROUP BY p.point_key "
    "HAVING MAX(w.retrieved_at) IS NULL OR julianday(MAX(w.retrieved_at)) < julianday(?) - ?")
AIS_ROWS_THIS_RUN = (
    "SELECT COUNT(*) FROM ais_position WHERE julianday(retrieved_at) >= julianday(?) "
    "AND julianday(retrieved_at) < julianday(?) + ?")
WAVE_ROWS_THIS_RUN = (
    "SELECT COUNT(*) FROM wave_reading WHERE julianday(retrieved_at) >= julianday(?) "
    "AND julianday(retrieved_at) < julianday(?) + ?")
AIS_ROWS_PER_EARLIER_FILE = (
    "SELECT COUNT(*) FROM ais_position GROUP BY source_file "
    "HAVING julianday(MAX(retrieved_at)) < julianday(?) ORDER BY julianday(MAX(retrieved_at)) DESC LIMIT ?")
RECENT_WAVE_GAPS = (
    "SELECT COUNT(*), MIN(prev), MIN(observed_at) FROM (SELECT point_key, observed_at, "
    "LAG(observed_at) OVER (PARTITION BY point_key ORDER BY observed_at) AS prev FROM wave_reading_clean) "
    "WHERE prev IS NOT NULL AND julianday(substr(observed_at, 1, 10) || ' ' || substr(observed_at, 12, 5)) "
    "- julianday(substr(prev, 1, 10) || ' ' || substr(prev, 12, 5)) > ? "
    "AND julianday(substr(observed_at, 1, 10) || ' ' || substr(observed_at, 12, 5)) >= julianday(?)")


def run(db_path=None, out=print, now=None):
    now = now or run_time()
    recent_start = (datetime.strptime(now, RUN_TIME_FMT) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d %H:%M")
    results = []

    def check(level, name, ok, detail):
        status = "PASS" if ok else level
        results.append(status)
        out("%-5s %-46s %s" % (status, name, detail))

    con = connect(db_path)
    try:
        con.execute("PRAGMA query_only = ON")
        # ---- STOP checks
        diffs = [(c, con.execute(q1).fetchone()[0], con.execute(q2).fetchone()[0]) for c, _, q1, q2 in PAIRS]
        check("STOP", "cleaned row count equals raw", all(a == b for _, a, b in diffs),
              ", ".join("%s %d = %d" % d for d in diffs))
        n_ais = con.execute(IMPOSSIBLE_AIS, (C.SOG_MAX, C.COG_MAX, C.HEADING_MAX, C.NAV_STATUS_MAX, C.ROT_MIN, C.ROT_MAX,
                                             C.REGION_BBOX[0], C.REGION_BBOX[2], C.REGION_BBOX[1], C.REGION_BBOX[3])).fetchone()[0]
        n_wave = con.execute(IMPOSSIBLE_WAVE, (C.WAVE_HEIGHT_MAX, C.DIRECTION_MAX, C.WAVE_PERIOD_MAX)).fetchone()[0]
        n_kept = con.execute(KEPT_INVALID).fetchone()[0]
        check("STOP", "no impossible cleaned value", n_ais + n_wave + n_kept == 0,
              "%d impossible ais, %d impossible wave, %d invalid-flagged values kept" % (n_ais, n_wave, n_kept))
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        check("STOP", "foreign_key_check empty", not fk, "OK" if not fk else "%d violating rows" % len(fk))
        n_raw = con.execute(RAW_MISMATCH).fetchone()[0]
        check("STOP", "_raw columns equal the M2 values", n_raw == 0, "%d mismatching rows" % n_raw)

        # ---- freshness, measured from the run time (M5): the whole feed stopping shows up here
        ais_last = con.execute(LAST_AIS_RETRIEVAL).fetchone()[0]
        if ais_last is None:
            check("WARN", "AIS feed newer than %d h" % FEED_MAX_AGE_HOURS, False, "no AIS rows at all")
        else:
            age = con.execute(HOURS_BEFORE, (now, ais_last)).fetchone()[0]
            check("WARN", "AIS feed newer than %d h" % FEED_MAX_AGE_HOURS, age <= FEED_MAX_AGE_HOURS,
                  "last retrieval %s, %.1f h before run time %s" % (ais_last, age, now))
        stale = con.execute(STALE_MARINE_POINTS, (now, FEED_MAX_AGE_HOURS / 24.0)).fetchall()
        check("WARN", "every marine point retrieved < %d h ago" % FEED_MAX_AGE_HOURS, not stale,
              "all points retrieved recently" if not stale else
              "stale points (point, last retrieval): %s" % [tuple(r) for r in stale])

        # ---- volume: rows from this run compared with a typical run (M5)
        window = RUN_WINDOW_MINUTES / 1440.0
        ais_now = con.execute(AIS_ROWS_THIS_RUN, (now, now, window)).fetchone()[0]
        earlier = [n for (n,) in con.execute(AIS_ROWS_PER_EARLIER_FILE, (now, VOLUME_HISTORY_FILES))]
        if len(earlier) < MIN_BASELINE_FILES:
            check("WARN", "AIS volume this run vs typical", ais_now > 0,
                  "%d rows this run; no baseline yet (%d earlier files)" % (ais_now, len(earlier)))
        else:
            typical = median(earlier)
            check("WARN", "AIS volume this run vs typical", 0 < ais_now <= VOLUME_FACTOR * typical,
                  "%d rows this run, typical %d (median of %d earlier files)" % (ais_now, typical, len(earlier)))
        wave_now = con.execute(WAVE_ROWS_THIS_RUN, (now, now, window)).fetchone()[0]
        check("WARN", "marine rows delivered this run", wave_now > 0, "%d readings from this run" % wave_now)

        # ---- WARN checks
        share = {k: s for k, s in con.execute(
            "SELECT point_key, AVG(wave_height_flag = 'missing_not_reported') FROM wave_reading_clean GROUP BY point_key")}
        bad = {k: round(s, 2) for k, s in share.items() if s > C.MAX_MISSING_SHARE}
        check("WARN", "missing share per marine point <= %d%%" % round(100 * C.MAX_MISSING_SHARE), not bad,
              "all %d points ok" % len(share) if not bad else "points over: %s" % bad)
        nocov = [k for (k,) in con.execute("SELECT DISTINCT point_key FROM wave_reading_clean "
                                           "WHERE wave_height_flag = 'missing_no_model_coverage'")]
        check("WARN", "every marine point has model coverage", not nocov,
              "all points covered" if not nocov else "no model data at points %s" % nocov)
        # per-entity version of freshness: one point going quiet in the latest hour of the feed
        latest = con.execute("SELECT MAX(observed_at) FROM wave_reading_clean").fetchone()[0]
        stale = [k for (k,) in con.execute(
            "SELECT point_key FROM marine_point WHERE point_key NOT IN "
            "(SELECT point_key FROM wave_reading_clean WHERE observed_at = ? AND wave_height IS NOT NULL)", (latest,))]
        check("WARN", "every point has a value in the latest hour", not stale,
              "latest hour %s" % latest if not stale else "latest hour %s, missing at points %s" % (latest, stale))
        # only recent gaps: old ones are history we cannot repair and would WARN on every run forever
        gaps = con.execute(RECENT_WAVE_GAPS, (C.MAX_GAP_HOURS / 24.0, recent_start)).fetchone()
        check("WARN", "no wave gap > %d h in the last %d days" % (C.MAX_GAP_HOURS, RECENT_DAYS), gaps[0] == 0,
              "no recent gaps" if gaps[0] == 0 else "%d gaps (first: %s -> %s)" % gaps)
        # INFO, not WARN: data properties already reported in docs/cleaning_report.md that never go away.
        # Downgraded on purpose; the reason must also be written in docs/pipeline.md.
        n_stuck = con.execute("SELECT COUNT(*) FROM wave_reading_clean WHERE wave_height_flag = 'stuck_value'").fetchone()[0]
        check("INFO", "stuck_value wave heights (history)", n_stuck == 0, "%d stuck_value flags" % n_stuck)
        n_wind = con.execute("SELECT COUNT(*) FROM wave_reading_clean "
                             "WHERE wind_wave_height_flag = 'inconsistent_exceeds_total'").fetchone()[0]
        check("WARN", "wind-wave height never exceeds total", n_wind == 0, "%d inconsistent rows" % n_wind)
        n_nav = con.execute("SELECT COUNT(*) FROM ais_position_clean "
                            "WHERE row_flag = 'nav_status_conflicts_with_sog'").fetchone()[0]
        check("INFO", "nav_status vs sog conflicts (history)", n_nav == 0, "%d conflicting positions" % n_nav)
        n_pos = con.execute("SELECT COUNT(*) FROM ais_position_clean "
                            "WHERE latitude IS NULL OR longitude IS NULL").fetchone()[0]
        check("WARN", "every AIS position has usable coordinates", n_pos == 0, "%d positions without a usable coordinate" % n_pos)
        keys = Counter(name_key(n) for (n,) in con.execute("SELECT ship_name FROM vessel_clean WHERE ship_name IS NOT NULL"))
        dup = sorted(k for k, c in keys.items() if c > 1)
        check("WARN", "no two vessels share a normalised name", not dup,
              "no candidates" if not dup else "identity candidates: %s" % dup)
        n_todo = con.execute("SELECT (SELECT COUNT(*) FROM nav_status_map WHERE decided_by LIKE 'TODO%') "
                             "+ (SELECT COUNT(*) FROM name_placeholder WHERE decided_by LIKE 'TODO%') "
                             "+ (SELECT COUNT(*) FROM vessel_alias WHERE decided_by LIKE 'TODO%')").fetchone()[0]
        check("WARN", "mapping rows name who decided", n_todo == 0, "%d rows still say TODO" % n_todo)
    except Exception as exc:
        out("STOP  validate.py could not run: %r (did you run clean.py?)" % (exc,))
        return 1
    finally:
        con.close()
    stops = results.count("STOP")
    out("%d PASS, %d INFO, %d WARN, %d STOP" % (results.count("PASS"), results.count("INFO"),
                                                  results.count("WARN"), stops))
    return 1 if stops else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--run-time", help="pipeline clock, e.g. 2026-11-20T08:10Z (default: PIPELINE_RUN_TIME or now)")
    args = ap.parse_args()
    if args.run_time:
        datetime.strptime(args.run_time, RUN_TIME_FMT)
    sys.exit(run(args.db, now=args.run_time))
