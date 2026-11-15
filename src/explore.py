"""Helpers for notebooks/m4_eda.ipynb: read-only connection, statistics, figure style.

Nothing here writes to the database. Every SQL statement is a fixed string; values travel as parameters.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
FIG_DIR = ROOT / "figures"
QUERY_DIR = ROOT / "sql" / "queries"
DB_DEFAULT = ROOT / os.environ.get("DB_PATH", "data/maritime.db")


# ---------------------------------------------------------------- database (read-only)
def connect_ro(path=None):
    """Open the database read-only: any INSERT/UPDATE/DELETE raises an error."""
    path = Path(path) if path else DB_DEFAULT
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def read_query(name):
    return (QUERY_DIR / name).read_text(encoding="utf-8")


def query(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def pad(values, n):
    """Pad a tuple of flags to the fixed arity of a query (repeat the last one)."""
    values = tuple(values)
    return values + (values[-1],) * (n - len(values))


def show(rows, cols=None):
    if not rows:
        print("(no rows)")
        return
    cols = cols or list(rows[0])
    w = [max(len(str(c)), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(cols)))
    print("  ".join("-" * x for x in w))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w[i]) for i, c in enumerate(cols)))


# ---------------------------------------------------------------- time
def local_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:           # no tz database: Turkey has been UTC+3 all year since 2016
        return timezone(timedelta(hours=3))


def parse_utc(t):
    """'2026-10-10T11:48:30.614Z' or '2026-10-10T11:00Z' -> aware UTC datetime."""
    fmt = "%Y-%m-%dT%H:%M:%S" if len(t) >= 19 else "%Y-%m-%dT%H:%M"
    return datetime.strptime(t[:19] if len(t) >= 19 else t[:16], fmt).replace(tzinfo=timezone.utc)


def to_local(t, tz):
    return parse_utc(t).astimezone(tz)


# ---------------------------------------------------------------- statistics
def pearson(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 3 or x.std() == 0 or y.std() == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def rankdata(a):
    a = np.asarray(a, float)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a))
    ranks[order] = np.arange(1, len(a) + 1)
    for v in np.unique(a):                       # average ranks for ties
        idx = a == v
        if idx.sum() > 1:
            ranks[idx] = ranks[idx].mean()
    return ranks


def spearman(x, y):
    return pearson(rankdata(x), rankdata(y))


def ecdf(values):
    v = np.sort(np.asarray(values, float))
    return v, np.arange(1, len(v) + 1) / len(v)


def bootstrap_diff(a, b, n_boot, seed, level=95):
    """Difference in means (a - b) with a percentile bootstrap interval; resampling the units in each group."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        diffs[i] = rng.choice(a, len(a)).mean() - rng.choice(b, len(b)).mean()
    lo, hi = np.percentile(diffs, [(100 - level) / 2, 100 - (100 - level) / 2])
    return float(a.mean() - b.mean()), float(lo), float(hi)


# ---------------------------------------------------------------- figures
plt.rcParams.update({"figure.dpi": 100, "savefig.dpi": 130, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.alpha": 0.25, "font.size": 9})


def finish(fig, name, title, subtitle, caption):
    """Title = the finding, subtitle = the question, caption = n, flag filter, clock. Written by code."""
    import textwrap
    FIG_DIR.mkdir(exist_ok=True)
    width = max(40, int(fig.get_figwidth() * 11))
    sub, ttl = textwrap.fill(subtitle, width + 10), textwrap.fill(title, width)
    ax = fig.axes[0]
    lift = 16 if ax.get_title(loc="left") else 0           # leave room for the panel's own small title
    ax.annotate(sub, xy=(0, 1), xycoords="axes fraction", xytext=(0, 6 + lift), textcoords="offset points",
                ha="left", va="bottom", fontsize=8.5, color="#444444")
    ax.annotate(ttl, xy=(0, 1), xycoords="axes fraction", xytext=(0, 12 + lift + 11 * (sub.count("\n") + 1)),
                textcoords="offset points", ha="left", va="bottom", fontsize=11.5, fontweight="bold")
    fig.text(0.01, -0.02, textwrap.fill(caption, width + 25), ha="left", va="top", fontsize=7.5, color="#444444")
    path = FIG_DIR / name
    fig.savefig(path, bbox_inches="tight")
    print("wrote", path.relative_to(ROOT))
    return path


def bootstrap_ci(values, n_boot, seed, level=95):
    """Percentile bootstrap interval of the mean of `values` (resampling the units)."""
    v = np.asarray(values, float)
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(v, len(v)).mean() for _ in range(n_boot)])
    return tuple(float(x) for x in np.percentile(means, [(100 - level) / 2, 100 - (100 - level) / 2]))
