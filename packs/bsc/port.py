#!/usr/bin/env python3
"""One-time knowledge port: vault -> db.

The hunt no longer ingests every campaign (the db persists what this writes).
Run it at setup, or whenever the vault changes:

    ./run-bsc.sh port

It ports findings/patterns/methods/chains (bsc.ingest_seed) and the code-note
readings (bsc.ingest_notes) into the shared db. Idempotent.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ENGINE = os.environ.get("ENGINE", str(Path.home() / "bsd/forgeflow"))
sys.path.insert(0, ENGINE)

from forgeflow import config, db                          # noqa: E402
from forgeflow.blocks import load_files, get              # noqa: E402
from forgeflow.util import tx                             # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("FF_ROOT", "../../run"))
    ap.add_argument("--pack", default=os.environ.get(
        "PACK_DIR", os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()

    pack = config.load_pack(args.pack)
    load_files(list(pack.block_files))
    conn = db.connect(str(Path(args.root) / "state" / "forgeflow.db"))
    step_dir = Path(args.root) / "data" / "port"
    step_dir.mkdir(parents=True, exist_ok=True)
    task = {"id": 0, "attempts": 0, "payload": {}}

    def ctx(**kw):
        kw.update(_conn=conn, _timeout_s=120, _step_dir=str(step_dir),
                  _tools=(pack.tools or {}))
        return kw

    with tx(conn):
        o1, r1 = get("bsc.ingest_seed").fn(
            ctx(repo=pack.paths["repo"], vault=pack.params["vault"]), task, {})
    print("ingest_seed :", o1, r1)

    notes = pack.paths.get("code_notes")
    if notes and Path(notes).is_dir():
        with tx(conn):
            o2, r2 = get("bsc.ingest_notes").fn(
                ctx(repo=pack.paths["repo"], notes_dir=notes), task, {})
        print("ingest_notes:", o2, r2)

    print("\ndb now holds:")
    for tbl in ("findings", "patterns", "methods", "chains", "readings", "regions"):
        try:
            n = conn.execute("SELECT count(*) FROM %s" % tbl).fetchone()[0]
        except Exception:
            n = "n/a"
        print("  %-10s %s" % (tbl, n))


if __name__ == "__main__":
    main()
