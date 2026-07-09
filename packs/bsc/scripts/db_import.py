#!/usr/bin/env python3
"""Rebuild a forgeflow DB from a knowledge export (db_export.py output).

Creates the DB from the self-contained SQL, then (best-effort) opens it through
the engine so the operational tables (events/tasks/...) are created empty and
the DB is immediately usable. Refuses to clobber a DB that already holds
findings unless --force.

  db_import.py [--sql data/forgeflow.knowledge.sql] [--db run/state/forgeflow.db] [--force]
"""
from __future__ import annotations

import argparse
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
    ap.add_argument("--sql", default="data/forgeflow.knowledge.sql")
    ap.add_argument("--db", default="run/state/forgeflow.db")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    sql = Path(a.sql)
    if not sql.is_file():
        sys.exit("no such export: %s" % sql)
    db = Path(a.db)
    if db.exists() and not a.force and _findings(db) > 0:
        sys.exit("refuse: %s already has %d findings — pass --force to overwrite"
                 % (db, _findings(db)))

    db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):                   # clear stale WAL sidecars
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()

    c = sqlite3.connect(db)
    c.executescript(sql.read_text())
    c.commit()
    n = c.execute("SELECT count(*) FROM findings").fetchone()[0]
    c.close()
    print("rebuilt %s from %s: %d findings" % (db, sql, n))

    # best-effort: let the engine create the operational tables it owns
    engine = os.environ.get("ENGINE", str(Path.home() / "bsd" / "forgeflow"))
    try:
        sys.path.insert(0, engine)
        from forgeflow import db as ffdb           # noqa: E402
        ffdb.connect(db).close()
        print("engine schema applied — operational tables created empty")
    except Exception as e:                          # not fatal: engine makes them on first run
        print("note: engine schema not applied (%s); the daemon will create "
              "operational tables on first run" % e)


if __name__ == "__main__":
    main()
