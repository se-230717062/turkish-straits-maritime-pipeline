#!/usr/bin/env python3
"""M5 runner: ingest -> load -> clean -> validate, honestly.

Run from anywhere:  python src/run_pipeline.py [--run-time 2026-11-20T08:10Z] [--skip-ingest] [--timeout SECONDS]

* every step is a separate process (a crash cannot take the runner down);
* the run stops at the first non-zero exit code and exits with that code (timeout = 124);
* every step that ran, including the failed one, is a row in data/pipeline_log.db (table pipeline_run);
* --run-time is the pipeline's clock and is passed to every step;
* --skip-ingest rebuilds the whole database from landed raw data, with no network;
* a lock file refuses a second run at the same time (exit code 75).

Exit codes: 0 success - 1 a step failed - 75 another run is in progress - 124 a step timed out - 127 a step could not start.
"""
import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from db import ROOT, format_run_time, parse_run_time

STEPS = ["src/ingest.py", "src/load.py", "src/clean.py", "src/validate.py"]
# seconds. ingest: AIS stream (AIS_COLLECT_SECONDS, default 600) + marine HTTP with retries (<= 5 x 30 s + back-off) = well under 1500
TIMEOUT_SECONDS = {"src/ingest.py": 1500, "src/load.py": 600, "src/clean.py": 600, "src/validate.py": 300}
LOG_DB = ROOT / os.environ.get("PIPELINE_LOG_DB", "data/pipeline_log.db")
LOCK_FILE = ROOT / os.environ.get("PIPELINE_LOCK", "data/pipeline.lock")
LOCK_STALE_SECONDS = 6 * 3600     # a lock whose owner process is gone, or that is older than this, is left over from a crash

EXIT_LOCKED, EXIT_TIMEOUT, EXIT_NOT_STARTED = 75, 124, 127

CREATE_LOG = """
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id      TEXT NOT NULL,
    run_time    TEXT NOT NULL,
    step        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    exit_code   INTEGER NOT NULL,
    detail      TEXT,
    PRIMARY KEY (run_id, step)
) STRICT
"""
INSERT_LOG = ("INSERT INTO pipeline_run (run_id, run_time, step, started_at, finished_at, exit_code, detail) "
              "VALUES (?, ?, ?, ?, ?, ?, ?)")


def wall() -> str:
    """Wall clock, ONLY for the log timestamps started_at / finished_at."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- lock
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_is_stale() -> bool:
    try:
        age = time.time() - LOCK_FILE.stat().st_mtime
        pid = int(LOCK_FILE.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return True
    return (not _pid_alive(pid)) or age > LOCK_STALE_SECONDS


def acquire_lock():
    """Create the lock with O_CREAT | O_EXCL. Returns True if we own it, False if another run is in progress."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale():
                try:
                    LOCK_FILE.unlink()
                except FileNotFoundError:
                    pass
                continue
            return False
        os.write(fd, ("%d\n" % os.getpid()).encode())
        os.close(fd)
        return True
    return False


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- run log
def log_connect() -> sqlite3.Connection:
    LOG_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(LOG_DB, isolation_level=None)
    con.execute(CREATE_LOG)
    columns = [row[1] for row in con.execute("PRAGMA table_info(pipeline_run)")]
    if "detail" not in columns:      # a log started by an earlier version of the runner: keep its rows, add the column
        con.execute("ALTER TABLE pipeline_run ADD COLUMN detail TEXT")
    return con


DETAIL_PATTERNS = {
    "src/ingest.py": [r"^OK\s+.*$", r"^FAILED\s+.*$"],
    "src/load.py": [r"^ais_positions: .*$", r"^wave_readings: .*$", r"^ERROR: .*$"],
    "src/clean.py": [r"^STATUS: .*$"],
    "src/validate.py": [r"^\d+ PASS, .*$", r"^STOP .*$"],
}


def detail_of(step: str, text: str, code: int) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    picked = []
    for pat in DETAIL_PATTERNS.get(step, []):
        picked += [ln for ln in lines if re.match(pat, ln)]
    if code == EXIT_TIMEOUT:
        picked.append("timeout")
    if not picked and lines:
        picked = [lines[-1]]
    return " | ".join(picked)[:500]


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def run_step(step: str, run_time: str, extra: list, timeout: int):
    """Run one step as a separate process. Returns (exit_code, output_text)."""
    cmd = [sys.executable, step, "--run-time", run_time] + extra
    try:
        cp = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return cp.returncode, _text(cp.stdout) + _text(cp.stderr)
    except subprocess.TimeoutExpired as exc:
        return EXIT_TIMEOUT, _text(exc.stdout) + _text(exc.stderr) + "\nTIMEOUT: %s did not finish in %d s and was stopped\n" % (step, timeout)
    except OSError as exc:
        return EXIT_NOT_STARTED, "could not start %s: %s\n" % (step, exc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-time", help="the pipeline's clock, e.g. 2026-11-20T08:10Z (default: now)")
    ap.add_argument("--skip-ingest", action="store_true", help="rebuild from landed raw data, no network")
    ap.add_argument("--timeout", type=int, help="override the timeout of every step, in seconds (for drills)")
    args = ap.parse_args()
    run_time = format_run_time(parse_run_time(args.run_time))
    run_id = uuid.uuid4().hex[:8]

    if not acquire_lock():
        print("PIPELINE NOT RUN: another run is in progress (lock %s). Nothing was run." % LOCK_FILE.name)
        return EXIT_LOCKED
    try:
        steps = [s for s in STEPS if not (args.skip_ingest and s == "src/ingest.py")]
        log = log_connect()
        print("PIPELINE START run_id=%s run_time=%s steps=%s%s" % (
            run_id, run_time, ",".join(Path(s).stem for s in steps), " (ingest skipped)" if args.skip_ingest else ""))
        for step in steps:
            extra = ["--rebuild"] if (step == "src/validate.py" and args.skip_ingest) else []
            timeout = args.timeout or TIMEOUT_SECONDS[step]
            started = wall()
            code, text = run_step(step, run_time, extra, timeout)
            log.execute(INSERT_LOG, (run_id, run_time, step, started, wall(), code, detail_of(step, text, code)))
            for line in text.rstrip().splitlines():
                print("  [%s] %s" % (Path(step).stem, line))
            if code != 0:
                print("PIPELINE FAILED run_id=%s at %s (exit code %d); later steps did not run" % (run_id, step, code))
                return code
        print("PIPELINE OK run_id=%s run_time=%s (%d steps, all exit code 0)" % (run_id, run_time, len(steps)))
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
