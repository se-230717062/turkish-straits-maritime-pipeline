#!/usr/bin/env python3
"""M5 runner: ingest -> load -> clean -> validate, each as a separate process.

Run from anywhere:  python src/run_pipeline.py [--run-time 2026-11-20T08:10Z] [--skip-ingest]

Exit code: 0 = success; otherwise the exit code of the first step that failed.
  75 = another run is in progress (nothing was run)
  124 = a step exceeded its timeout
Every step that ran (including a failed one) gets one row in data/pipeline_log.db.
"""
import argparse
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

from db import ROOT, connect

LOG_DB = ROOT / "data" / "pipeline_log.db"
LOCK = ROOT / "data" / "pipeline.lock"
TIME_FMT = "%Y-%m-%dT%H:%MZ"        # the pipeline's clock
STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"    # wall-clock log timestamps

EXIT_LOCKED = 75
EXIT_TIMEOUT = 124

# (script, timeout in seconds). ingest.py collects AIS for 600 s by default,
# plus retries for the weather API, so it gets the longest timeout.
STEPS = [
    ("src/ingest.py", 1200),
    ("src/load.py", 900),
    ("src/clean.py", 900),
    ("src/validate.py", 300),
]

CREATE_LOG = """
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id      TEXT NOT NULL,
    run_time    TEXT NOT NULL,
    step        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    exit_code   INTEGER NOT NULL,
    PRIMARY KEY (run_id, step)
) STRICT
"""
INSERT_LOG = ("INSERT INTO pipeline_run (run_id, run_time, step, started_at, finished_at, exit_code) "
              "VALUES (?, ?, ?, ?, ?, ?)")


def wall_stamp() -> str:
    """Wall clock: only for started_at / finished_at."""
    return datetime.now(timezone.utc).strftime(STAMP_FMT)


# ---------------------------------------------------------------- lock
def holder_alive() -> bool:
    """Is the process that wrote the lock file still running?"""
    try:
        pid = int(LOCK.read_text().strip())
    except FileNotFoundError:
        return False
    except ValueError:
        return True  # file exists but is empty/half-written: another run is starting
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock() -> bool:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if holder_alive():
                return False
            LOCK.unlink(missing_ok=True)  # stale lock: a run was killed or the Mac shut down
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


# ---------------------------------------------------------------- steps
def run_step(step: str, run_time: str) -> int:
    env = dict(os.environ, PIPELINE_RUN_TIME=run_time)  # steps will read this clock later
    try:
        return subprocess.run([sys.executable, step], cwd=ROOT, env=env,
                              timeout=dict(STEPS)[step]).returncode
    except subprocess.TimeoutExpired:
        print("TIMEOUT: %s did not finish in %d s" % (step, dict(STEPS)[step]))
        return EXIT_TIMEOUT


def main() -> int:
    ap = argparse.ArgumentParser(description="Run ingest -> load -> clean -> validate.")
    ap.add_argument("--run-time", help="pipeline clock, e.g. 2026-11-20T08:10Z (default: now, UTC)")
    ap.add_argument("--skip-ingest", action="store_true", help="rebuild from landed raw data, no network")
    args = ap.parse_args()

    run_time = args.run_time or datetime.now(timezone.utc).strftime(TIME_FMT)  # the clock's only origin
    try:
        datetime.strptime(run_time, TIME_FMT)
    except ValueError:
        ap.error("--run-time must look like 2026-11-20T08:10Z")

    if not acquire_lock():
        print("Another pipeline run is in progress (%s). Nothing was run." % LOCK)
        return EXIT_LOCKED

    con = None
    try:
        con = connect(LOG_DB)
        con.execute(CREATE_LOG)
        run_id = uuid.uuid4().hex[:8]
        print("run %s, run_time %s" % (run_id, run_time))
        for step, _ in (STEPS[1:] if args.skip_ingest else STEPS):
            started = wall_stamp()
            print("\n=== %s ===" % step, flush=True)
            code = run_step(step, run_time)
            con.execute(INSERT_LOG, (run_id, run_time, step, started, wall_stamp(), code))
            if code != 0:
                print("\nPIPELINE FAILED at %s (exit code %d)" % (step, code))
                return code
        print("\nPIPELINE OK (run %s)" % run_id)
        return 0
    finally:
        if con is not None:
            con.close()
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
