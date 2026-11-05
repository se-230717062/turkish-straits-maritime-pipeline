#!/usr/bin/env python3
"""M3 report: cleaning log, anchorage membership and the two analytical queries.

Run from the repository root:  python src/clean_report.py [--db path]
Read-only. Run `python src/load.py` first (it creates the views and anchorage tables).
"""
import argparse
import sys

from db import ROOT, connect

QUERIES = ["sql/queries/wave_vs_speed.sql", "sql/queries/wave_vs_speed_anchorage.sql"]


def show(con, title, sql):
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    print("\n== %s ==" % title)
    if not rows:
        print("(no rows)")
        return
    w = [max(len(str(x)) for x in [c] + [r[i] for r in rows]) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w[i]) for i, c in enumerate(cols)))
    print("  ".join("-" * x for x in w))
    for r in rows:
        print("  ".join(str(v).ljust(w[i]) for i, v in enumerate(r)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    args = ap.parse_args()
    con = connect(args.db)
    con.execute("PRAGMA query_only = ON")
    show(con, "cleaning log (raw rows changed per rule)", "SELECT * FROM cleaning_log")
    show(con, "positions inside each anchorage",
         "SELECT a.name, COUNT(*) AS n_positions, COUNT(DISTINCT ia.vessel_key) AS n_vessels "
         "FROM ais_in_anchorage ia JOIN anchorage a USING (anchorage_key) GROUP BY a.anchorage_key")
    show(con, "anchorage -> wave point (nearest to centroid)",
         "SELECT a.name, m.latitude AS point_lat, m.longitude AS point_lon, ROUND(d.dist2, 4) AS dist2 "
         "FROM anchorage a LEFT JOIN anchorage_wave_point d USING (anchorage_key) "
         "LEFT JOIN marine_point m USING (point_key)")
    overlap = con.execute("SELECT COUNT(*) FROM (SELECT 1 FROM ais_in_anchorage "
                          "GROUP BY vessel_key, observed_at HAVING COUNT(*) > 1)").fetchone()[0]
    print("\npositions in more than one anchorage (must be 0): %d" % overlap)
    for q in QUERIES:
        show(con, q, (ROOT / q).read_text(encoding="utf-8"))
    return 1 if overlap else 0


if __name__ == "__main__":
    sys.exit(main())
