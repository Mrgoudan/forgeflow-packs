#!/usr/bin/env python3
"""Prove the manual-wins-on-change rule deterministically (no model needed).

Builds a fake manual git repo, then shows the bsc_manual provider return:
  pinned == HEAD  -> status 'current'  (trust the bsc-* skills)
  manual moves    -> status 'CHANGED', authoritative, changed section text
                     (the manual overrides any skill that disagrees)

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

import bsc  # noqa: E402  (registers the providers)
from forgeflow.contract import CONTEXT_PROVIDERS  # noqa: E402


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd)] + list(a), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    work = HERE / "demo-bsc-run"
    shutil.rmtree(str(work), ignore_errors=True)
    work.mkdir()
    manual = work / "manual"
    (manual / "src" / "chapter-3-memory-safety").mkdir(parents=True)
    ownership = manual / "src" / "chapter-3-memory-safety" / "ownership.md"
    ownership.write_text("# Ownership\nAn owned pointer is freed once.\n")
    git(manual, "init", "-q")
    git(manual, "config", "user.email", "d@d.invalid")
    git(manual, "config", "user.name", "d")
    git(manual, "add", "-A")
    git(manual, "commit", "-qm", "manual v1")
    rev1 = subprocess.run(["git", "-C", str(manual), "rev-parse", "HEAD"],
                          stdout=subprocess.PIPE).stdout.decode().strip()

    provider = CONTEXT_PROVIDERS["bsc_manual"]

    def env_for(pinned):
        pack = SimpleNamespace(
            paths={"manual": str(manual)},
            params={"manual_pinned_rev": pinned},
            tools={})
        return SimpleNamespace(pack=pack, data_dir=str(work / "data"))

    task = {"id": 1, "attempts": 0, "payload": {}}

    print("=== manual UNCHANGED (pinned == HEAD) ===")
    r1 = provider(env_for(rev1), task, {})
    print(json.dumps({k: r1[k] for k in ("status", "note")}, indent=1))
    assert r1["status"] == "current"

    # the manual moves: ownership rule refined
    ownership.write_text("# Ownership\nAn owned pointer is freed once; a "
                         "moved-from pointer must not be freed AGAIN.\n")
    git(manual, "commit", "-aqm", "manual v2: clarify double-free on move")

    print("\n=== manual CHANGED (pinned still v1) ===")
    r2 = provider(env_for(rev1), task, {})
    print("status:", r2["status"], " authoritative:", r2["authoritative"])
    print("note:", r2["note"])
    print("changed_files:", r2["changed_files"])
    print("authoritative section text:")
    print("  " + r2["changed_sections"][0]["text"].replace("\n", "\n  "))
    assert r2["status"] == "CHANGED" and r2["authoritative"] is True
    assert any("moved-from" in s["text"] for s in r2["changed_sections"])

    print("\nMANUAL-WINS RULE: OK  (unchanged -> trust skills;"
          " changed -> manual authoritative, overrides skills)")


if __name__ == "__main__":
    main()
