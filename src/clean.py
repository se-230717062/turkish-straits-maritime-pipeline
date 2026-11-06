#!/usr/bin/env python3
"""M3: build the cleaned layer from the M2 tables.

Run from the repository root:  python src/load.py && python src/clean.py

Reads ONLY the M2 tables (never raw files, never the API). Writes ONLY the cleaned tables and the
mapping tables. The M2 tables are hashed before and after: if they differ, everything is rolled back.
All thresholds live in src/clean_config.py. Nothing is imputed: a value is NULL when not trusted.

Rule order (first rule that applies wins, per value):
  numeric AIS fields : missing -> sentinel -> out of range -> (sog only) unusual_high -> ok
  latitude/longitude : missing -> sentinel -> out of range -> null island -> outside region -> ok
  wave_height        : missing -> negative -> out of range -> stuck_value (>= STUCK_MIN_M) -> spike -> unusual_high -> ok
  other wave fields  : missing -> out of range / negative -> (wind) inconsistent_exceeds_total -> ok
  ship_name          : missing -> placeholder_name -> normalised -> ok
  mmsi               : invalid_length -> not_a_ship -> ok
"""
import csv
import sqlite3
import statistics
import sys
import types
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import clean_config as C
from db import ROOT, connect, table_digest, transaction

CLEAN_SCHEMA = ROOT / "sql" / "clean_schema.sql"
MAPPING_DIR = ROOT / "data" / "mappings"
RAW_TABLES = ("vessel", "marine_point", "ais_position", "wave_reading")
HOUR = timedelta(hours=1)

# Fixed, literal statements: values only ever travel through placeholders.
CLEAN_DUMP_SQL = {
    "vessel_clean": "SELECT * FROM vessel_clean ORDER BY vessel_key",
    "ais_position_clean": "SELECT * FROM ais_position_clean ORDER BY vessel_key, observed_at",
    "wave_reading_clean": "SELECT * FROM wave_reading_clean ORDER BY point_key, observed_at",
    "nav_status_map": "SELECT * FROM nav_status_map ORDER BY code",
    "name_placeholder": "SELECT * FROM name_placeholder ORDER BY name_key",
    "vessel_alias": "SELECT * FROM vessel_alias ORDER BY alias_mmsi",
}


def _ph(n):
    return ",".join(["?"] * n)


INS_VESSEL = "INSERT INTO vessel_clean (vessel_key, mmsi_raw, mmsi, mmsi_flag, ship_name_raw, ship_name, ship_name_flag) VALUES (" + _ph(7) + ")"
INS_AIS = ("INSERT INTO ais_position_clean (vessel_key, observed_at, latitude_raw, latitude, latitude_flag, "
           "longitude_raw, longitude, longitude_flag, sog_raw, sog, sog_flag, cog_raw, cog, cog_flag, "
           "true_heading_raw, true_heading, true_heading_flag, nav_status_raw, nav_status, nav_status_flag, "
           "rate_of_turn_raw, rate_of_turn, rate_of_turn_flag, row_flag) VALUES (" + _ph(24) + ")")
INS_WAVE = ("INSERT INTO wave_reading_clean (point_key, observed_at, wave_height_raw, wave_height, wave_height_flag, "
            "wave_direction_raw, wave_direction, wave_direction_flag, wave_period_raw, wave_period, wave_period_flag, "
            "wind_wave_height_raw, wind_wave_height, wind_wave_height_flag) VALUES (" + _ph(14) + ")")
INS_MAP = {
    "nav_status_map": ("INSERT INTO nav_status_map (code, label, motion_group, decided_by, decided_at, note) VALUES (" + _ph(6) + ")",
                       ("code", "label", "motion_group", "decided_by", "decided_at", "note"), (int,) + (str,) * 5),
    "name_placeholder": ("INSERT INTO name_placeholder (name_key, decided_by, decided_at, note) VALUES (" + _ph(4) + ")",
                         ("name_key", "decided_by", "decided_at", "note"), (str,) * 4),
    "vessel_alias": ("INSERT INTO vessel_alias (alias_mmsi, canonical_mmsi, kind, decided_by, decided_at, note) VALUES (" + _ph(6) + ")",
                     ("alias_mmsi", "canonical_mmsi", "kind", "decided_by", "decided_at", "note"), (int, int) + (str,) * 4),
}

CSV_FILE = {"nav_status_map": "nav_status.csv", "name_placeholder": "name_placeholder.csv",
            "vessel_alias": "vessel_alias.csv"}
DELETE_MAP = {"nav_status_map": "DELETE FROM nav_status_map", "name_placeholder": "DELETE FROM name_placeholder",
              "vessel_alias": "DELETE FROM vessel_alias"}

_TR = str.maketrans({"İ": "i", "ı": "i", "I": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
                     "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c"})


def tidy_name(s):
    """Display form: NFC, non-breaking spaces and the AIS '@' padding become spaces, whitespace collapsed."""
    s = unicodedata.normalize("NFC", s).replace("\u00a0", " ").replace("@", " ")
    return " ".join(s.split())


def name_key(s):
    """Matching key: NFC, then fold Turkish letters, then casefold (never str.lower())."""
    return tidy_name(s).translate(_TR).casefold()


def run_script(con, path):
    buf = ""
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            con.execute(buf)
            buf = ""
    if buf.strip() and not all(l.strip().startswith("--") or not l.strip() for l in buf.splitlines()):
        raise RuntimeError("incomplete statement at end of %s" % path.name)


# ---------------------------------------------------------------- generic rules
def classify(raw, sentinel, lo, hi):
    """missing -> sentinel -> out of range -> ok. Returns (cleaned, flag)."""
    if raw is None:
        return None, "missing_not_reported"
    if sentinel is not None and raw == sentinel:
        return None, "invalid_sentinel"
    if raw < lo or raw > hi:
        return None, "invalid_out_of_range"
    return raw, "ok"


def classify_wave(raw, lo, hi, covered, negative_flag):
    if raw is None:
        return None, "missing_not_reported" if covered else "missing_no_model_coverage"
    if raw < lo:
        return None, "invalid_negative" if negative_flag else "invalid_out_of_range"
    if raw > hi:
        return None, "invalid_out_of_range"
    return raw, "ok"


def parse_hour(t):
    return datetime.strptime(t[:16], "%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------- mappings
def load_mappings(con, say):
    """Reload every mapping table from its committed CSV (DELETE + INSERT, inside the run's transaction)."""
    counts = {}
    for table, (sql, cols, types_) in INS_MAP.items():
        path = MAPPING_DIR / CSV_FILE[table]
        rows = []
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in cols if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError("%s: missing columns %s" % (path.name, missing))
            for n, rec in enumerate(reader, start=2):
                vals = []
                for c, cast in zip(cols, types_):
                    v = (rec[c] or "").strip()
                    if v == "":
                        raise ValueError("%s line %d: empty %s" % (path.name, n, c))
                    vals.append(cast(v))
                if table == "name_placeholder":
                    vals[0] = name_key(vals[0])
                rows.append(tuple(vals))
        con.execute(DELETE_MAP[table])
        con.executemany(sql, rows)
        counts[table] = len(rows)
    return counts


# ---------------------------------------------------------------- vessels
def build_vessels(con, cfg, placeholders):
    out = []
    for vk, mmsi, name in con.execute("SELECT vessel_key, mmsi, ship_name FROM vessel ORDER BY vessel_key").fetchall():
        s = str(mmsi)
        if len(s) != cfg.MMSI_LENGTH:
            m, mf = None, "invalid_length"
        elif s[0] not in cfg.MMSI_SHIP_FIRST_DIGITS:
            m, mf = mmsi, "not_a_ship"
        else:
            m, mf = mmsi, "ok"
        if name is None or tidy_name(name) == "":
            n, nf = None, "missing_not_reported"
        elif name_key(name) in placeholders:
            n, nf = None, "placeholder_name"
        else:
            n = tidy_name(name)
            nf = "ok" if n == name else "normalised"
        out.append((vk, mmsi, m, mf, name, n, nf))
    return out


# ---------------------------------------------------------------- AIS positions
def build_ais(con, cfg, stationary_codes):
    rows = con.execute("SELECT vessel_key, observed_at, latitude, longitude, sog, cog, true_heading, nav_status, "
                       "rate_of_turn FROM ais_position ORDER BY vessel_key, observed_at").fetchall()
    lat_min, lon_min, lat_max, lon_max = cfg.REGION_BBOX
    stage = []
    for vk, t, lat, lon, sog, cog, hdg, nav, rot in rows:
        la, laf = classify(lat, cfg.SENTINEL_LAT, -90.0, 90.0)
        lo, lof = classify(lon, cfg.SENTINEL_LON, -180.0, 180.0)
        if lat == 0 and lon == 0:
            la, laf, lo, lof = None, "invalid_null_island", None, "invalid_null_island"
        if laf == "ok" and not (lat_min <= la <= lat_max):
            la, laf = None, "invalid_outside_region"
        if lof == "ok" and not (lon_min <= lo <= lon_max):
            lo, lof = None, "invalid_outside_region"
        s, sf = classify(sog, cfg.SENTINEL_SOG, 0.0, cfg.SOG_MAX)
        c, cf = classify(cog, cfg.SENTINEL_COG, 0.0, cfg.COG_MAX)
        h, hf = classify(hdg, cfg.SENTINEL_HEADING, 0, cfg.HEADING_MAX)
        n, nf = classify(nav, cfg.SENTINEL_NAV_STATUS, 0, cfg.NAV_STATUS_MAX)
        r, rf = classify(rot, cfg.SENTINEL_ROT, cfg.ROT_MIN, cfg.ROT_MAX)
        stage.append([vk, t, lat, la, laf, lon, lo, lof, sog, s, sf, cog, c, cf, hdg, h, hf, nav, n, nf, rot, r, rf])
    valid = [r[9] for r in stage if r[10] == "ok"]
    if len(valid) >= 4:
        q1, _, q3 = statistics.quantiles(valid, n=4, method="inclusive")
        if q3 > q1:
            fence = q3 + cfg.IQR_K * (q3 - q1)
            for r in stage:
                if r[10] == "ok" and r[9] > fence:
                    r[10] = "unusual_high"
    out = []
    for r in stage:
        conflict = r[18] is not None and r[18] in stationary_codes and r[9] is not None and r[9] >= cfg.ANCHORED_MAX_SOG
        out.append(tuple(r) + ("nav_status_conflicts_with_sog" if conflict else "ok",))
    return out


# ---------------------------------------------------------------- wave readings
def build_waves(con, cfg):
    rows = con.execute("SELECT point_key, observed_at, wave_height, wave_direction, wave_period, wind_wave_height "
                       "FROM wave_reading ORDER BY point_key, observed_at").fetchall()
    by_point = defaultdict(list)
    for i, r in enumerate(rows):
        by_point[r[0]].append(i)
    height, hflag = [None] * len(rows), [""] * len(rows)
    res = [None] * len(rows)
    for idx in by_point.values():
        covered = [any(rows[i][c] is not None for i in idx) for c in (2, 3, 4, 5)]
        for i in idx:
            _, _, h, d, p, w = rows[i]
            hv, hf = classify_wave(h, 0.0, cfg.WAVE_HEIGHT_MAX, covered[0], True)
            dv, df = classify_wave(d, 0.0, cfg.DIRECTION_MAX, covered[1], False)
            pv, pf = classify_wave(p, 0.0, cfg.WAVE_PERIOD_MAX, covered[2], False)
            wv, wf = classify_wave(w, 0.0, cfg.WAVE_HEIGHT_MAX, covered[3], True)
            if hv is not None and wv is not None and wv > hv + cfg.WIND_TOL_M:
                wf = "inconsistent_exceeds_total"
            height[i], hflag[i] = hv, hf
            res[i] = [d, dv, df, p, pv, pf, w, wv, wf]
        times = {i: parse_hour(rows[i][1]) for i in idx}
        # stuck runs (identical non-zero value, consecutive hours)
        run = []

        def close(run):
            if len(run) >= cfg.STUCK_RUN and height[run[0]] >= cfg.STUCK_MIN_M:
                for j in run:
                    hflag[j] = "stuck_value"
        for i in idx:
            v = height[i]
            if v is None:
                close(run)
                run = []
            elif run and v == height[run[-1]] and times[i] - times[run[-1]] == HOUR:
                run.append(i)
            else:
                close(run)
                run = [i]
        close(run)
        # spikes: higher than BOTH hourly neighbours by more than SPIKE_M
        for k in range(1, len(idx) - 1):
            i, a, b = idx[k], idx[k - 1], idx[k + 1]
            if hflag[i] == "ok" and height[a] is not None and height[b] is not None \
                    and times[i] - times[a] == HOUR and times[b] - times[i] == HOUR \
                    and height[i] - height[a] > cfg.SPIKE_M and height[i] - height[b] > cfg.SPIKE_M:
                hflag[i] = "spike"
        # unusual (kept): robust z per point, median and MAD
        vals = [height[i] for i in idx if height[i] is not None]
        if len(vals) >= 8:
            med = statistics.median(vals)
            mad = statistics.median(abs(v - med) for v in vals)
            if mad > 0:
                for i in idx:
                    if hflag[i] == "ok" and height[i] >= cfg.UNUSUAL_MIN_M \
                            and 0.6745 * (height[i] - med) / mad > cfg.ROBUST_Z:
                        hflag[i] = "unusual_high"
    out = []
    for i, r in enumerate(rows):
        d, dv, df, p, pv, pf, w, wv, wf = res[i]
        out.append((r[0], r[1], r[2], height[i], hflag[i], d, dv, df, p, pv, pf, w, wv, wf))
    return out


# ---------------------------------------------------------------- summary
def flag_table(say, title, counts):
    flags = sorted({f for c in counts.values() for f in c})
    say(title)
    width = max(len(k) for k in counts) if counts else 10
    say("  " + "".ljust(width) + "  " + "  ".join(flags))
    for k, c in counts.items():
        say("  " + str(k).ljust(width) + "  " + "  ".join(str(c.get(f, 0)).rjust(len(f)) for f in flags))


def clean_digest(con):
    import hashlib
    out = {}
    for t, sql in CLEAN_DUMP_SQL.items():
        h = hashlib.sha256()
        for row in con.execute(sql):
            h.update(repr(row).encode("utf-8"))
        out[t] = h.hexdigest()
    return out


def run(db_path=None, out=print, **overrides):
    cfg = types.SimpleNamespace(**{k: v for k, v in vars(C).items() if k.isupper()})
    for k, v in overrides.items():
        if not hasattr(cfg, k):
            raise KeyError("unknown constant %s" % k)
        setattr(cfg, k, v)
    lines = []
    say = lines.append
    con = connect(db_path)
    try:
        for t in RAW_TABLES:
            if con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone()[0] != 1:
                raise RuntimeError("M2 table %s not found - run python src/load.py first" % t)
        with transaction(con):
            raw_before = table_digest(con)
            run_script(con, CLEAN_SCHEMA)
            # rebuild: cleaned tables only, never an M2 table
            con.execute("DELETE FROM ais_position_clean")
            con.execute("DELETE FROM wave_reading_clean")
            con.execute("DELETE FROM vessel_clean")
            map_counts = load_mappings(con, say)
            placeholders = {k for (k,) in con.execute("SELECT name_key FROM name_placeholder")}
            stationary = {c for (c,) in con.execute("SELECT code FROM nav_status_map WHERE motion_group = ?", ("stationary",))}

            vessels = build_vessels(con, cfg, placeholders)
            con.executemany(INS_VESSEL, vessels)
            ais = build_ais(con, cfg, stationary)
            con.executemany(INS_AIS, ais)
            waves = build_waves(con, cfg)
            con.executemany(INS_WAVE, waves)

            if table_digest(con) != raw_before:
                raise RuntimeError("an M2 table changed during cleaning")
            n_raw = {t: con.execute(q).fetchone()[0] for t, q in (
                ("vessel", "SELECT COUNT(*) FROM vessel"), ("ais_position", "SELECT COUNT(*) FROM ais_position"),
                ("wave_reading", "SELECT COUNT(*) FROM wave_reading"))}
            if (len(vessels), len(ais), len(waves)) != (n_raw["vessel"], n_raw["ais_position"], n_raw["wave_reading"]):
                raise RuntimeError("a cleaned table has a different row count than its raw table")

            # ---- summary
            say("vessel_clean: %d rows (raw: %d)" % (len(vessels), n_raw["vessel"]))
            flag_table(say, "", {"mmsi": Counter(v[3] for v in vessels), "ship_name": Counter(v[6] for v in vessels)})
            say("ais_position_clean: %d rows (raw: %d)" % (len(ais), n_raw["ais_position"]))
            cols = (("latitude", 4), ("longitude", 7), ("sog", 10), ("cog", 13), ("true_heading", 16),
                    ("nav_status", 19), ("rate_of_turn", 22), ("row_flag", 23))
            flag_table(say, "", {name: Counter(r[i] for r in ais) for name, i in cols})
            say("wave_reading_clean: %d rows (raw: %d)" % (len(waves), n_raw["wave_reading"]))
            flag_table(say, "", {name: Counter(r[i] for r in waves) for name, i in
                                 (("wave_height", 4), ("wave_direction", 7), ("wave_period", 10), ("wind_wave_height", 13))})
            per_point = defaultdict(Counter)
            for r in waves:
                per_point[r[0]][r[4]] += 1
            flag_table(say, "wave_height flags per marine point (point_key):",
                       {"point %s" % k: per_point[k] for k in sorted(per_point)})

            # ---- mapping bookkeeping: which entries matched nothing
            mmsis = {m for (m,) in con.execute("SELECT mmsi FROM vessel")}
            bad_alias = [a for a, c in con.execute("SELECT alias_mmsi, canonical_mmsi FROM vessel_alias")
                         if a not in mmsis or c not in mmsis]
            keys_seen = {name_key(v[4]) for v in vessels if v[4]}
            unused_ph = sorted(p for p in placeholders if p not in keys_seen)
            codes_seen = {r[17] for r in ais if r[17] is not None}
            unused_codes = sorted(c for c in {c for (c,) in con.execute("SELECT code FROM nav_status_map")} if c not in codes_seen)
            say("mappings loaded: " + ", ".join("%s %d rows" % (t, n) for t, n in map_counts.items()))
            say("  vessel_alias entries matching no vessel: %s" % (bad_alias or "none"))
            say("  name_placeholder entries matching no vessel: %d of %d" % (len(unused_ph), len(placeholders)))
            say("  nav_status codes never seen in the data: %s" % (unused_codes or "none"))
            say("M2 tables unchanged (sha256 of every table equal before and after)")
        for line in lines:
            out(line)
        out("STATUS: COMPLETE")
        return 0
    except Exception as exc:
        out("clean.py FAILED: %r" % (exc,))
        out("STATUS: FAILED (the cleaned layer was left as it was before this run)")
        return 1
    finally:
        con.close()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    return run(ap.parse_args().db)


if __name__ == "__main__":
    sys.exit(main())
