"""Port the campaign's ACCUMULATED KNOWLEDGE into native db rows.

HUNT.md is a port spec; the real system (autotest/sem_tests) carries the
knowledge the hunt runs on — without it the hunt is blind (re-discovers
known bugs, has no playbook to classify against). This block ingests, from
the vault:

- findings.jsonl        -> findings   (the dedup catalogue: ~142 F-ids)
- code_notes/_playbook  -> patterns   (defect classes C1..C12 the explorer
                                        classifies against; the grep bench)
- code_notes/_methods   -> methods    (the real detection-method bench)
- code_notes/_chains    -> chains      (call-chain surfaces A..N, Mode 2)

Idempotent (INSERT OR IGNORE / upsert by id). Run once at setup, or whenever
the vault advances.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from forgeflow.blocks import block
from forgeflow.util import template

# status(finding) -> finding-state, so imported bugs land in a sensible state
# and don't clutter the board as 142 "open" findings.
_STATE = {"filed": "pr_open", "fixed": "merged", "unfiled": "found",
          "retracted": "rejected", "do-not-file": "rejected"}

_CLASS = re.compile(r"^\|\s*(C\d+)\s+([^|]+?)\s*\|\s*(\w[\w-]*)\s*\|\s*(.*?)\s*\|\s*$")
_CHAIN = re.compile(r"^###\s+Chain\s+([A-Z])\s+[—-]\s+(.+?)\s*$")

# _methods.md is heterogeneous: a legacy DEMOTED table (col3 = "status"),
# the current ACTIVE bench (col3 = "reaches class"), and per-round ARM-RESULT
# tables (| arm | n | genuine |) that hold the bandit's accumulated priors.
_M_DEF = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|")   # backticked-id def row
_MH_ARM = re.compile(r"^\|\s*arm\s*\|\s*n\s*\|\s*genuine\s*\|", re.I)
_MH_STATUS = re.compile(r"^\|\s*id\s*\|[^|]*\|\s*status\s*\|", re.I)
_MH_ID = re.compile(r"^\|\s*id\s*\|", re.I)
_M_SEP = re.compile(r"^\|\s*-+")
_M_ARMROW = re.compile(r"^\|([^|]+)\|\s*\**(\d+)\**\s*\|\s*\**(\d+)\**\s*\|")


@block("bsc.ingest_seed", "state", {"ok", "error"},
       required_params={"repo", "vault"})
def bsc_ingest_seed(ctx, task, prev):
    """Port the campaign's accumulated KNOWLEDGE from the vault into native db
    rows, so the hunt starts where the last campaign left off (idempotent, run
    at campaign start):
      findings.jsonl -> findings   (the dedup catalogue: known defects)
      _playbook.md   -> patterns   (defect classes C1..C12)
      _methods.md    -> methods    (the bandit bench + warm-start trial/yield)
      _chains.md     -> chains      (call-chain surfaces)
    """
    conn = ctx["_conn"]
    repo = template(ctx["repo"], {})
    vault = Path(template(ctx["vault"], {}))
    notes = vault / "code_notes"
    out = {}

    # --- findings.jsonl -> findings (dedup catalogue) --------------------
    fj = vault / "findings.jsonl"
    n_f = 0
    if fj.is_file():
        for line in fj.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            fid = d.get("id")
            if not fid:
                continue
            state = _STATE.get(d.get("status", ""), "found")
            title = (" ".join(x for x in (fid, d.get("feature"),
                                          d.get("severity")) if x))[:200]
            conn.execute(
                "INSERT OR IGNORE INTO findings(key, source, title, detail,"
                " severity, repo, state) VALUES (?,?,?,?,?,?,?)",
                (fid, "import", title, json.dumps(d),
                 (d.get("severity") or "").lower() or None, repo, state))
            n_f += 1
    out["findings"] = n_f

    # --- _playbook.md -> patterns (defect classes) ----------------------
    n_p = 0
    pb = notes / "_playbook.md"
    if pb.is_file():
        for ln in pb.read_text(errors="replace").splitlines():
            m = _CLASS.match(ln)
            if not m:
                continue
            cid, name, status, exemplars = m.groups()
            lens = "%s (%s): %s" % (name.strip(), status, exemplars.strip())
            conn.execute(
                "INSERT INTO patterns(id, description, review_lens, status)"
                " VALUES (?,?,?, 'active') ON CONFLICT(id) DO UPDATE SET"
                " description=excluded.description, review_lens=excluded.review_lens",
                (cid, name.strip(), lens[:1000]))
            n_p += 1
    out["patterns"] = n_p

    # --- _methods.md -> methods bench (+ bandit priors) -----------------
    # Header-state parser: a legacy DEMOTED table seeds status='exhausted'
    # (kept for provenance, excluded from the bandit); the ACTIVE bench seeds
    # status='active'; ARM-RESULT tables warm-start trials/verified_yield so
    # the bandit doesn't re-cycle known high-volume/low-yield methods.
    n_m, priors = 0, {}
    mm = notes / "_methods.md"
    if mm.is_file():
        demoted = in_arm = False
        for ln in mm.read_text(errors="replace").splitlines():
            if not ln.strip():                       # blank line ends a table
                demoted = in_arm = False
                continue
            if _MH_ARM.match(ln):
                in_arm, demoted = True, False
                continue
            if _MH_STATUS.match(ln):
                demoted, in_arm = True, False
                continue
            if _MH_ID.match(ln):
                demoted = in_arm = False
                continue
            if _M_SEP.match(ln):
                continue
            if in_arm:
                a = _M_ARMROW.match(ln)
                if a:
                    aid = re.sub(r"[*`]", "", a.group(1)).strip()
                    p = priors.setdefault(aid, [0, 0])
                    p[0] += int(a.group(2)); p[1] += int(a.group(3))
                continue
            m = _M_DEF.match(ln)
            if m:
                mid, desc = m.group(1).strip(), m.group(2).strip()
                conn.execute(
                    "INSERT INTO methods(id, description, status) VALUES (?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET description=excluded.description,"
                    " status=excluded.status",
                    (mid, desc[:400], "exhausted" if demoted else "active"))
                n_m += 1
    for aid, (t, g) in priors.items():               # warm-start the bandit
        conn.execute("UPDATE methods SET trials=trials+?, verified_yield=verified_yield+?"
                     " WHERE id=?", (t, g, aid))
    out["methods"] = n_m
    out["method_priors"] = len(priors)

    # --- _chains.md -> chains (Mode 2 surfaces) -------------------------
    n_c = 0
    ch = notes / "_chains.md"
    if ch.is_file():
        for ln in ch.read_text(errors="replace").splitlines():
            m = _CHAIN.match(ln)
            if not m:
                continue
            cid, desc = m.group(1), m.group(2)
            untraced = "UNTRACED" in desc
            conn.execute(
                "INSERT OR IGNORE INTO chains(id, repo, sha, nodes,"
                " hop_invariants, status) VALUES (?,?, 'seed', '[]', ?, ?)",
                (cid, repo, json.dumps({"summary": desc.strip()[:300]}),
                 "candidate" if untraced else "active"))
            n_c += 1
    out["chains"] = n_c

    return "ok", out
