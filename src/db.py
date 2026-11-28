"""Shared database helpers. Every script connects through connect()."""
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / os.environ.get("DB_PATH", "data/maritime.db")
SCHEMA_FILE = ROOT / "sql" / "schema.sql"

# Fixed, literal statements: no SQL is ever built from strings.
COUNT_SQL = {
    "vessel": "SELECT COUNT(*) FROM vessel",
    "marine_point": "SELECT COUNT(*) FROM marine_point",
    "ais_position": "SELECT COUNT(*) FROM ais_position",
    "wave_reading": "SELECT COUNT(*) FROM wave_reading",
    "quarantine": "SELECT COUNT(*) FROM quarantine",
}
DUMP_SQL = {
    "vessel": "SELECT * FROM vessel ORDER BY vessel_key",
    "marine_point": "SELECT * FROM marine_point ORDER BY point_key",
    "ais_position": "SELECT * FROM ais_position ORDER BY vessel_key, observed_at",
    "wave_reading": "SELECT * FROM wave_reading ORDER BY point_key, observed_at",
    "quarantine": "SELECT * FROM quarantine ORDER BY quarantine_key",
}


def connect(path=None, create=False) -> sqlite3.Connection:
    """Open the database. foreign_keys is switched on immediately.

    A database that should already exist is never created by accident: with create=False (the default)
    a missing file is an error. Only load.py, which builds the database, passes create=True.
    isolation_level=None -> we control transactions explicitly (see transaction()).
    """
    path = Path(path) if path else DEFAULT_DB
    if not path.exists():
        if not create:
            raise FileNotFoundError("database %s does not exist (run src/load.py first)" % path)
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA foreign_keys = ON")
    if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("could not enable foreign key enforcement")
    return con


# ---------------------------------------------------------------- the pipeline's clock (M5)
RUN_TIME_FORMAT = "%Y-%m-%dT%H:%MZ"


def parse_run_time(value=None) -> datetime:
    """The pipeline's clock: '2026-11-20T08:10Z' (or with seconds) -> aware UTC datetime.

    With value=None it returns the current time to the minute. This is the ONE place where a run reads the
    wall clock; every step receives the result through --run-time and decides everything from it.
    """
    if value is None:
        return datetime.now(timezone.utc).replace(second=0, microsecond=0)
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError("run time must look like 2026-11-20T08:10Z, got %r" % value)


def format_run_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(RUN_TIME_FORMAT)


def drill(step: str) -> None:
    """Failure drills (M5, section 6). OFF unless PIPELINE_DRILL is set, e.g. PIPELINE_DRILL=load=fail,clean=hang.

    fail -> the step crashes with an exception (exit code 1); hang -> the step sleeps until the runner's timeout.
    """
    for item in os.environ.get("PIPELINE_DRILL", "").split(","):
        name, _, mode = item.strip().partition("=")
        if name == step and mode == "fail":
            raise RuntimeError("drill: injected failure in step %r" % step)
        if name == step and mode == "hang":
            time.sleep(3600)


@contextmanager
def transaction(con):
    """One atomic unit: COMMIT on success, ROLLBACK on any exception (DDL included)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def init_schema(con):
    """Run sql/schema.sql statement by statement (idempotent; transactional)."""
    buf = ""
    for line in SCHEMA_FILE.read_text(encoding="utf-8").splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            con.execute(buf)
            buf = ""
    if buf.strip() and not all(l.strip().startswith("--") or not l.strip() for l in buf.splitlines()):
        raise RuntimeError("incomplete statement at end of schema.sql")


def table_counts(con) -> dict:
    return {t: con.execute(sql).fetchone()[0] for t, sql in COUNT_SQL.items()}


def table_digest(con) -> dict:
    """Content fingerprint of every table (for idempotence/atomicity checks)."""
    out = {}
    for t, sql in DUMP_SQL.items():
        h = hashlib.sha256()
        for row in con.execute(sql):
            h.update(repr(row).encode("utf-8"))
        out[t] = h.hexdigest()
    return out
