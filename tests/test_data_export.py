from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ENGINE, PACKS


class DataExportTest(unittest.TestCase):
    """The committed data/knowledge export must rebuild a DB whose schema
    matches the CURRENT code. A stale export (finding_id columns left over from
    the de-leak rename) rebuilt DBs that broke PR reviews — the _history
    provider queries implications.item_id. This guards against that recurring."""

    def test_export_rebuilds_with_current_columns(self):
        tmp = Path(tempfile.mkdtemp()) / "rt.db"
        env = {**os.environ, "ENGINE": str(ENGINE),
               "PYTHONPATH": str(ENGINE) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        r = subprocess.run(
            [sys.executable, str(PACKS / "packs/bsc/scripts/db_import.py"),
             "--dir", str(PACKS / "data/knowledge"), "--db", str(tmp), "--force"],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        c = sqlite3.connect(tmp)
        for t in ("implications", "transitions"):
            cols = [row[1] for row in c.execute("PRAGMA table_info(%s)" % t)]
            self.assertIn("item_id", cols, "%s missing item_id — stale export?" % t)
            self.assertNotIn("finding_id", cols, "%s still has finding_id" % t)
        self.assertGreater(
            c.execute("SELECT count(*) FROM items").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
