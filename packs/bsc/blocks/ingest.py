"""Seed ingestion — port the file-based code notes into NATIVE db rows.

The code notes under the seed dir are prior knowledge (imported from the
predecessor). This block ingests them the way the system natively stores
what it learns: one `code_objects` row per source file + one `readings`
row carrying the note's digest and content. After ingestion the review's
context comes from the db (readings), indistinguishable from readings the
agent produces itself — no file-folder dependency.

Idempotent: re-running updates the same rows (keyed by object + sha 'seed').
"""
from __future__ import annotations

import re
from pathlib import Path

from forgeflow.blocks import block

# INDEX.md rows:  | `NoteFile.md` | `clang/lib/.../Source.cpp` | status |
_INDEX_ROW = re.compile(r"^\|\s*`([^`]+\.md)`\s*\|\s*`([^`]+?)`")
_SUMMARY_CAP = 400
_FACTS_CAP = 8000


@block("bsc.ingest_notes", "state", {"ok", "error"},
       required_params={"notes_dir", "repo"})
def bsc_ingest_notes(ctx, task, prev):
    conn = ctx["_conn"]
    notes_dir = Path(ctx["notes_dir"])
    repo = str(ctx["repo"])
    index = notes_dir / "INDEX.md"
    if not index.is_file():
        return "error", {"reason": "no INDEX.md in %s" % notes_dir}

    mapping = {}
    for line in index.read_text(errors="replace").splitlines():
        m = _INDEX_ROW.match(line)
        if m:
            # source may carry a "(qualifier)" — keep the path only
            src = m.group(2).split(" ")[0].strip()
            mapping.setdefault(m.group(1), src)

    ingested = 0
    for note_file, source in sorted(mapping.items()):
        p = notes_dir / note_file
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        summary = _digest(text)
        # native: code_object for the source file
        row = conn.execute(
            "SELECT id FROM code_objects WHERE repo=? AND path=? AND symbol IS NULL",
            (repo, source)).fetchone()
        if row:
            obj_id = row["id"]
            conn.execute("UPDATE code_objects SET last_seen_sha='seed' WHERE id=?",
                         (obj_id,))
        else:
            obj_id = conn.execute(
                "INSERT INTO code_objects(repo, path, symbol, kind,"
                " first_seen_sha, last_seen_sha) VALUES (?,?,NULL,'file','seed','seed')",
                (repo, source)).lastrowid
        # native: one 'seed' reading per object (replace on re-ingest)
        conn.execute("DELETE FROM readings WHERE object_id=? AND sha='seed'", (obj_id,))
        conn.execute(
            "INSERT INTO readings(object_id, run_id, sha, summary, facts)"
            " VALUES (?, NULL, 'seed', ?, ?)",
            (obj_id, summary[:_SUMMARY_CAP], text[:_FACTS_CAP]))
        ingested += 1
    return "ok", {"ingested": ingested, "source_files": len(mapping)}


def _digest(text):
    """First heading + first real paragraph — the short digest for prompts."""
    lines = [l.strip() for l in text.splitlines()]
    head = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), "")
    para = ""
    for l in lines:
        if l and not l.startswith("#") and not l.startswith("Source:"):
            para = l
            break
    return ("%s — %s" % (head, para)).strip(" —")
