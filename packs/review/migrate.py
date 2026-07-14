"""Pack-owned columns on the core items table.

The engine core no longer carries forge bookkeeping (branch, pr_number) — that
is review/fix-pack vocabulary. The engine's schema apply is CREATE IF NOT
EXISTS (new tables yes, new columns never), so the pack adds its columns here:
idempotent, safe to run at every launch and in every test fixture.

Deployment: run-bsc.sh calls scripts/migrate_db.py (which calls this) before
exec'ing the engine. Tests: helpers.pack_db() wraps db.connect() with it.
"""
from __future__ import annotations

# (table, column, ALTER) — append-only.
MIGRATIONS = [
    ("items", "branch",
     "ALTER TABLE items ADD COLUMN branch TEXT"),
    ("items", "pr_number",
     "ALTER TABLE items ADD COLUMN pr_number INTEGER"),
]


def add_pack_columns(conn):
    """Apply every missing pack-owned column. Returns the ones added."""
    applied = []
    for table, col, ddl in MIGRATIONS:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                            " AND name=?", (table,)).fetchone():
            continue
        cols = {r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)}
        if col not in cols:
            conn.execute(ddl)
            applied.append("%s.%s" % (table, col))
    return applied
