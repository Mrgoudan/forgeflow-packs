#!/usr/bin/env python3
"""Prove the two ground-truth rules deterministically (no model needed),
with the manual living INSIDE the reviewed repo:

  bsc_manual provider:
    manual blob == pinned  -> status 'current' (trust skills)
    manual blob != pinned  -> status 'CHANGED' (manual overrides skills)
  bsc.manual_gate block:
    PR touches BSC semantics but NOT the manual -> machine finding
    PR touches semantics AND the manual         -> ok

Usage: ENGINE=~/bsd/forgeflow python3 scripts/demo_bsc_manual.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
ENGINE = Path(os.environ.get("ENGINE", Path.home() / "bsd" / "forgeflow"))
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(HERE / "bsc" / "blocks"))

import bsc  # noqa: E402 (registers provider + block)
from forgeflow.blocks import run_isolated  # noqa: E402
from forgeflow.contract import CONTEXT_PROVIDERS  # noqa: E402

MANUAL = "clang/docs/BSC/BiShengCLanguageUserManual.md"
SEMА = "clang/lib/Sema/BSC/SemaBSC.cpp"


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd)] + list(a), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def blob(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          stdout=subprocess.PIPE).stdout.decode().strip()


def main():
    work = HERE / "demo-bsc-run"
    shutil.rmtree(str(work), ignore_errors=True)
    work.mkdir()
    repo = work / "repo"
    (repo / "clang/docs/BSC").mkdir(parents=True)
    (repo / "clang/lib/Sema/BSC").mkdir(parents=True)
    (repo / MANUAL).write_text("# BiSheng C Manual\nAn owned pointer is freed once.\n")
    (repo / SEMА).write_text("// sema\n")
    git(repo, "init", "-q")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "config", "user.email", "d@d.invalid")
    git(repo, "config", "user.name", "d")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    pinned = blob(repo, "main:" + MANUAL)

    def env_for():
        pack = SimpleNamespace(
            paths={"repo": str(repo)},
            params={"manual_path": MANUAL, "manual_pinned_sha": pinned},
            tools={})
        return SimpleNamespace(pack=pack, data_dir=str(work / "data"))

    prov = CONTEXT_PROVIDERS["bsc_manual"]

    # branch A: updates semantics AND the manual
    git(repo, "checkout", "-qb", "good")
    (repo / SEMА).write_text("// sema v2: reject double free\n")
    (repo / MANUAL).write_text("# BiSheng C Manual\nAn owned pointer is freed "
                               "once; freeing a moved-from pointer is UB.\n")
    git(repo, "commit", "-aqm", "semantics + manual")
    git(repo, "checkout", "-q", "main")

    # branch B: updates semantics but NOT the manual
    git(repo, "checkout", "-qb", "bad")
    (repo / SEMА).write_text("// sema v3: silent change\n")
    git(repo, "commit", "-aqm", "semantics only")
    git(repo, "checkout", "-q", "main")

    print("=== bsc_manual on branch 'good' (manual updated) ===")
    r = prov(env_for(), {"payload": {"branch": "good"}}, {})
    print("status:", r["status"], " authoritative:", r["authoritative"])
    assert r["status"] == "CHANGED"          # manual differs from pinned
    assert "moved-from" in r["excerpt"]

    print("=== bsc_manual on 'main' (manual == pinned) ===")
    r0 = prov(env_for(), {"payload": {"branch": "main"}}, {})
    print("status:", r0["status"])
    assert r0["status"] == "current"

    print("\n=== manual_gate on 'good' (semantics + manual) -> ok ===")
    out, res = run_isolated(
        "bsc.manual_gate",
        {"repo": str(repo), "manual_path": MANUAL,
         "semantics_prefixes": ["clang/lib/Sema/BSC"], "_tools": {}},
        task={"id": 1, "attempts": 0,
              "payload": {"base": "main", "branch": "good"}},
        prev={"path": "/ws"})
    print("outcome:", out, " staged:", "_staged" in res)
    assert out == "ok" and "_staged" not in res

    print("=== manual_gate on 'bad' (semantics, NO manual) -> flagged ===")
    out, res = run_isolated(
        "bsc.manual_gate",
        {"repo": str(repo), "manual_path": MANUAL,
         "semantics_prefixes": ["clang/lib/Sema/BSC"], "_tools": {}},
        task={"id": 2, "attempts": 0,
              "payload": {"base": "main", "branch": "bad"}},
        prev={"path": "/ws"})
    print("outcome:", out)
    print("finding:", res["_staged"][0]["title"])
    assert out == "flagged"
    assert res["_staged"][0]["key"] == "pattern-bad-manual-not-updated"

    print("\nBSC GROUND-TRUTH RULES: OK")
    print("  manual (in-repo) changed -> overrides skills")
    print("  manual unchanged         -> trust skills")
    print("  semantics changed w/o manual update -> machine finding (must update first)")


if __name__ == "__main__":
    main()
