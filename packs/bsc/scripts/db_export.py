#!/usr/bin/env python3
"""Export the forgeflow DB's KNOWLEDGE to a stable, git-friendly SQL text file.

The DB is the living source of truth; this is its portable, reviewable, and
transferable projection. Only the knowledge tables are dumped — the
operational/audit tables (events, tasks, task_steps, runs) and regenerable ones
(embeddings, dash_control) are omitted; the engine re-creates those empty on
first open. Rows are emitted in primary-key order so re-exports produce minimal
git diffs. Self-contained (schema + data): rebuild with db_import.py.

  db_export.py [--db run/state/forgeflow.db] [--out data/forgeflow.knowledge.sql]
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

# what carries the campaign's accumulated knowledge (worth versioning)
KNOWLEDGE = ["code_objects", "findings", "transitions", "patterns", "methods",
             "chains", "readings", "regions", "coverage", "implications",
             "lessons", "egress", "watermarks"]
# deliberately omitted: events/tasks/task_steps/runs (operational audit),
# embeddings (regenerable vectors), dash_control (machine-local UI flags).


def _sql(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return "X'%s'" % v.hex()
    return "'%s'" % str(v).replace("'", "''")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="run/state/forgeflow.db")
    ap.add_argument("--out", default="data/forgeflow.knowledge.sql")
    a = ap.parse_args()

    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row
    have = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "-- forgeflow knowledge export (schema + data, self-contained).",
        "-- Source of truth is the DB; this is its git-tracked projection.",
        "-- Regenerate: run-bsc.sh export   Rebuild a DB: run-bsc.sh import",
        "-- Operational/regenerable tables (events/tasks/runs/embeddings/...) are",
        "-- intentionally omitted; the engine re-creates them empty on open.",
        "PRAGMA foreign_keys=OFF;",
        "BEGIN;",
    ]
    rows_total, tabs = 0, 0
    for t in KNOWLEDGE:
        if t not in have:
            continue
        ddl = c.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                        " AND name=?", (t,)).fetchone()
        if not ddl or not ddl[0]:
            continue
        info = list(c.execute("PRAGMA table_info(%s)" % t))
        cols = [r[1] for r in info]
        pk = [r[1] for r in info if r[5]]                 # r[5] = pk position
        order = ", ".join(pk) if pk else ", ".join(cols)  # stable diff order
        tabs += 1
        lines += ["", "DROP TABLE IF EXISTS %s;" % t, ddl[0].strip() + ";"]
        collist = ", ".join(cols)
        for row in c.execute("SELECT * FROM %s ORDER BY %s" % (t, order)):
            vals = ", ".join(_sql(row[col]) for col in cols)
            lines.append("INSERT INTO %s (%s) VALUES (%s);" % (t, collist, vals))
            rows_total += 1
    lines.append("COMMIT;")
    out.write_text("\n".join(lines) + "\n")
    print("exported %d rows across %d tables -> %s (%.0f KB)"
          % (rows_total, tabs, out, out.stat().st_size / 1e3))


if __name__ == "__main__":
    main()
