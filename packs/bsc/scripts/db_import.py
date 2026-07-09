#!/usr/bin/env python3
"""Rebuild a forgeflow DB from a CHUNKED knowledge export (db_export.py output:
`_schema.sql` + one `<table>.jsonl` per table).

Creates the DB, applies the schema, loads every table, then opens it through the
engine so the operational tables (events/tasks/...) are created empty. Refuses
to clobber a DB that already holds findings unless --force.

  db_import.py [--dir data/knowledge] [--db run/state/forgeflow.db] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def _findings(db):
    try:
        c = sqlite3.connect(db)
        n = c.execute("SELECT count(*) FROM findings").fetchone()[0]
        c.close()
        return n
    except sqlite3.OperationalError:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/knowledge")
    ap.add_argument("--db", default="run/state/forgeflow.db")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    d = Path(a.dir)
    if not (d / "_schema.sql").is_file():
        sys.exit("no export at %s (missing _schema.sql)" % d)
    db = Path(a.db)
    if db.exists() and not a.force and _findings(db) > 0:
        sys.exit("refuse: %s already has %d findings — pass --force to overwrite"
                 % (db, _findings(db)))

    db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()

    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys=OFF")            # tables load in any order
    c.executescript((d / "_schema.sql").read_text())
    total = 0
    for f in sorted(d.glob("*.jsonl")):
        t = f.stem
        cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
        if not cols:
            continue
        ph = ", ".join("?" * len(cols))
        rows = [[json.loads(line).get(col) for col in cols]
                for line in f.read_text().splitlines() if line.strip()]
        c.executemany("INSERT INTO %s (%s) VALUES (%s)"
                      % (t, ", ".join(cols), ph), rows)
        total += len(rows)
    c.commit()
    n = c.execute("SELECT count(*) FROM findings").fetchone()[0]
    c.close()
    print("rebuilt %s from %s: %d rows (%d findings)" % (db, d, total, n))

    engine = os.environ.get("ENGINE", str(Path.home() / "bsd" / "forgeflow"))
    try:
        sys.path.insert(0, engine)
        from forgeflow import db as ffdb            # noqa: E402
        ffdb.connect(db).close()
        print("engine schema applied — operational tables created empty")
    except Exception as e:
        print("note: engine schema not applied (%s); the daemon will create "
              "operational tables on first run" % e)


if __name__ == "__main__":
    main()
