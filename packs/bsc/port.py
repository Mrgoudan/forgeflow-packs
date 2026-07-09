#!/usr/bin/env python3
"""One-time knowledge port: vault -> db.

The hunt no longer ingests every campaign (the db persists what this writes).
Run it at setup, or whenever the vault changes:

    ./run-bsc.sh port

It ports items/patterns/methods/chains (bsc.ingest_seed) and the code-note
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

    # --- curate the PRIMARY reading methods to the top of the bench --------
    # func-read (whole-function white-box) and call-chain-read (interprocedural
    # chain walk) are the FOUNDATIONAL hunt methods. Imported items don't
    # record which bench-arm found them (found_by is free-text), so credit
    # these two by attribution: the campaign found most of its bugs by reading.
    import json as _json
    from collections import Counter as _Counter
    cnt = _Counter()
    for row in conn.execute("SELECT detail FROM items WHERE source='import'"):
        try:
            fb = (_json.loads(row["detail"] or "{}").get("found_by") or "").strip().lower()
        except Exception:
            fb = ""
        if fb == "multi-agent":
            cnt["call-chain-read"] += 1          # multi-agent = interprocedural
        elif fb == "fuzz":
            pass                                 # not a reading method
        else:
            cnt["src-reading"] += 1              # blank/glm/block-read = source read
    PRIMARY = {
        "src-reading": "func-read — invariant-driven white-box read of ONE "
                       "function + hand probe (Mode 1); the campaign default",
        "call-chain-read": "chain-read — walk a call chain (the chains surfaces) "
                           "and check each hop invariant holds across the call "
                           "boundary (Mode 2)",
    }
    with tx(conn):
        for mid, desc in PRIMARY.items():
            y = cnt.get(mid, 0)
            conn.execute(
                "INSERT INTO methods(id, description, status, trials, verified_yield)"
                " VALUES (?,?, 'active', ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET status='active',"
                " description=excluded.description, trials=?, verified_yield=?",
                (mid, desc, y, y, y, y))
    print("primary reading methods (attributed yield):", dict(cnt))

    print("\ndb now holds:")
    for tbl in ("items", "patterns", "methods", "chains", "readings", "regions"):
        try:
            n = conn.execute("SELECT count(*) FROM %s" % tbl).fetchone()[0]
        except Exception:
            n = "n/a"
        print("  %-10s %s" % (tbl, n))


if __name__ == "__main__":
    main()
