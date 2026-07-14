#!/usr/bin/env python3
"""Bring an existing state DB up to the packs' schema: pack-owned columns
(the engine's IF-NOT-EXISTS schema apply adds tables, never columns).
Idempotent; run-bsc.sh calls it before exec'ing the engine.

  python3 migrate_db.py --db <state.db>
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review"))
import migrate as review_migrate  # noqa: E402  (packs/review/migrate.py)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    a = ap.parse_args()
    conn = sqlite3.connect(a.db)
    applied = review_migrate.add_pack_columns(conn)
    conn.commit()
    print("migrated: %s" % (", ".join(applied) if applied else "nothing to do"))
