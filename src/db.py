"""Shared database helpers. Every script connects through connect()."""
import hashlib
import os
import sqlite3
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


def connect(path=None) -> sqlite3.Connection:
    """Open the database. foreign_keys is switched on immediately.

    isolation_level=None -> we control transactions explicitly (see transaction()).
    """
    path = Path(path) if path else DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA foreign_keys = ON")
    if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("could not enable foreign key enforcement")
    return con


RUN_TIME_FMT = "%Y-%m-%dT%H:%MZ"


def run_time() -> str:
    """The pipeline's clock as ISO 8601 UTC, e.g. '2026-11-20T08:10Z'.

    run_pipeline.py sets PIPELINE_RUN_TIME for every step, so all steps of one run
    share one clock. When a script is started by hand without it, fall back to now.
    Every "since", "last 24 hours" or "today" decision must use this, never datetime.now().
    """
    value = os.environ.get("PIPELINE_RUN_TIME")
    if value:
        datetime.strptime(value, RUN_TIME_FMT)  # raises ValueError on a malformed clock
        return value
    return datetime.now(timezone.utc).strftime(RUN_TIME_FMT)


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
