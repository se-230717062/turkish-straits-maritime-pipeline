#!/usr/bin/env python3
"""M3 validation: expectations on the cleaned layer, as code. PASS / WARN / STOP per check.

Run from the repository root:  python src/validate.py [--run-time 2026-11-20T08:10Z] [--rebuild]
Exit code 1 if any STOP fails. WARNs are printed every time but do not stop the pipeline.

M5: freshness and recency are measured from the run time (--run-time), not from MAX(observed_at), so a feed that
stopped is seen. History-based WARNs only look at the last RECENT_DAYS before the run time (no permanent WARNs).
--rebuild (used by run_pipeline.py --skip-ingest) turns the freshness and volume checks into INFO: a rebuild from
landed files is not expected to have fresh data.
"""
import argparse
import statistics
import sys
from collections import Counter
from datetime import timedelta

import clean_config as C
from clean import name_key
from db import connect, drill, format_run_time, parse_run_time

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


def run(db_path=None, out=print, run_time=None, rebuild=False):
    now = parse_run_time(None) if run_time is None else run_time
    since = now - timedelta(days=C.RECENT_DAYS)
    now_w, since_w = format_run_time(now), format_run_time(since)          # wave times: 2026-11-20T08:10Z
    now_a, since_a = now_w[:16], since_w[:16]                                # AIS times start the same way, then add seconds
    run_hour = now.strftime("%Y-%m-%dT%H:00Z")
    results = []

    def check(level, name, ok, detail):
        if rebuild and level == "WARN" and name.startswith(("fresh", "volume")):
            level = "INFO"
        status = "PASS" if ok else level
        results.append(status)
        out("%-5s %-46s %s" % (status, name, detail))

    con = connect(db_path)
    try:
        con.execute("PRAGMA query_only = ON")
        out("run time (pipeline clock): %s%s" % (now_w, "  [rebuild: freshness and volume are INFO]" if rebuild else ""))
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

        # ---- freshness measured from the run time
        last_ais = con.execute("SELECT MAX(retrieved_at) FROM ais_position").fetchone()[0]
        last_wav = con.execute("SELECT MAX(retrieved_at) FROM wave_reading").fetchone()[0]
        for label, last in (("AIS", last_ais), ("marine", last_wav)):
            if last is None:
                check("WARN", "fresh: %s feed landed recently" % label, False, "no %s data at all" % label)
                continue
            age = (now - parse_run_time(last[:16] + "Z")).total_seconds() / 3600.0
            check("WARN", "fresh: %s file at most %dh old" % (label, C.MAX_FEED_AGE_HOURS), 0 <= age <= C.MAX_FEED_AGE_HOURS,
                  "newest %s file retrieved %s, %.1f h before the run time" % (label, last[:16] + "Z", age))
        stale = [k for (k,) in con.execute(
            "SELECT point_key FROM marine_point WHERE point_key NOT IN "
            "(SELECT point_key FROM wave_reading_clean WHERE observed_at = ? AND wave_height IS NOT NULL)", (run_hour,))]
        check("WARN", "fresh: every point has a value at the run hour", not stale,
              "all points have %s" % run_hour if not stale else "no value at %s for points %s" % (run_hour, stale))

        # ---- volume: rows landed by the latest run against a typical earlier run (AIS: every landed file is new rows)
        groups = [n for _, n in con.execute("SELECT retrieved_at, COUNT(*) FROM ais_position GROUP BY retrieved_at ORDER BY retrieved_at")]
        if len(groups) > C.VOLUME_MIN_HISTORY:
            latest, typical = groups[-1], statistics.median(groups[:-1])
            ok = typical > 0 and typical / C.VOLUME_FACTOR <= latest <= typical * C.VOLUME_FACTOR
            check("WARN", "volume: AIS rows of the latest run", ok,
                  "%d rows against a typical %.0f (median of %d earlier runs)" % (latest, typical, len(groups) - 1))
        else:
            check("WARN", "volume: AIS rows of the latest run", True, "only %d landed files so far, not enough history" % len(groups))

        # ---- the M3 checks, now relative to the run time (recent window only)
        share = {k: s_ for k, s_ in con.execute(
            "SELECT point_key, AVG(wave_height_flag = 'missing_not_reported') FROM wave_reading_clean GROUP BY point_key")}
        bad = {k: round(s_, 2) for k, s_ in share.items() if s_ > C.MAX_MISSING_SHARE}
        check("WARN", "missing share per marine point <= %d%%" % round(100 * C.MAX_MISSING_SHARE), not bad,
              "all %d points ok" % len(share) if not bad else "points over: %s" % bad)
        nocov = [k for (k,) in con.execute("SELECT DISTINCT point_key FROM wave_reading_clean "
                                           "WHERE wave_height_flag = 'missing_no_model_coverage'")]
        check("WARN", "every marine point has model coverage", not nocov,
              "all points covered" if not nocov else "no model data at points %s" % nocov)
        gaps = con.execute(
            "SELECT COUNT(*), MIN(prev), MIN(observed_at) FROM (SELECT point_key, observed_at, "
            "LAG(observed_at) OVER (PARTITION BY point_key ORDER BY observed_at) AS prev FROM wave_reading_clean "
            "WHERE observed_at >= ? AND observed_at <= ?) "
            "WHERE prev IS NOT NULL AND julianday(substr(observed_at, 1, 10) || ' ' || substr(observed_at, 12, 5)) "
            "- julianday(substr(prev, 1, 10) || ' ' || substr(prev, 12, 5)) > ?", (since_w, now_w, C.MAX_GAP_HOURS / 24.0)).fetchone()
        check("WARN", "no wave gap > %d h in the last %d days" % (C.MAX_GAP_HOURS, C.RECENT_DAYS), gaps[0] == 0,
              "no gaps" if gaps[0] == 0 else "%d gaps (first: %s -> %s)" % gaps)
        n_stuck = con.execute("SELECT COUNT(*) FROM wave_reading_clean WHERE wave_height_flag = 'stuck_value' "
                              "AND observed_at >= ? AND observed_at <= ?", (since_w, now_w)).fetchone()[0]
        check("WARN", "no new stuck_value wave heights (%d d)" % C.RECENT_DAYS, n_stuck == 0, "%d stuck_value flags" % n_stuck)
        n_wind = con.execute("SELECT COUNT(*) FROM wave_reading_clean WHERE wind_wave_height_flag = 'inconsistent_exceeds_total' "
                             "AND observed_at >= ? AND observed_at <= ?", (since_w, now_w)).fetchone()[0]
        check("WARN", "wind-wave height never exceeds total (%d d)" % C.RECENT_DAYS, n_wind == 0, "%d inconsistent rows" % n_wind)
        n_nav = con.execute("SELECT COUNT(*) FROM ais_position_clean WHERE row_flag = 'nav_status_conflicts_with_sog' "
                            "AND observed_at >= ? AND observed_at <= ?", (since_a, now_a + "~")).fetchone()[0]
        check("WARN", "nav_status agrees with sog (%d d)" % C.RECENT_DAYS, n_nav == 0, "%d conflicting positions" % n_nav)
        n_pos = con.execute("SELECT COUNT(*) FROM ais_position_clean WHERE (latitude IS NULL OR longitude IS NULL) "
                            "AND observed_at >= ? AND observed_at <= ?", (since_a, now_a + "~")).fetchone()[0]
        check("WARN", "every recent AIS position has coordinates", n_pos == 0, "%d positions without a usable coordinate (%d d)" % (n_pos, C.RECENT_DAYS))
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
    out("%d PASS, %d WARN, %d STOP, %d INFO" % (results.count("PASS"), results.count("WARN"), stops, results.count("INFO")))
    return 1 if stops else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--run-time", help="the pipeline's clock, e.g. 2026-11-20T08:10Z (default: now)")
    ap.add_argument("--rebuild", action="store_true", help="a rebuild from landed files: freshness and volume are INFO")
    args = ap.parse_args()
    drill("validate")
    sys.exit(run(args.db, run_time=parse_run_time(args.run_time), rebuild=args.rebuild))
