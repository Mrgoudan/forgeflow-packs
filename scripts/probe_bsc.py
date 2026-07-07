#!/usr/bin/env python3
"""Probe the BSC reviewer with a deterministic fake agent (no GLM key). Two
scenarios prove the policy "BSC must be AI-reviewed; if the AI breaks down,
queue for another run":

  A. AI works    -> full pipeline: gate, prescan, lens, refute, adjudicate,
                    review.completed. Findings vetted; manual injected
                    authoritative.
  B. AI down     -> the review PARKS (re-queued by the daemon's unpark tick),
                    machine findings stay 'found' (filed, not posted); NO
                    machine-only degrade. Unpark re-runs it.

Usage: ENGINE=~/bsd/forgeflow python3 scripts/probe_bsc.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ENGINE = Path(os.environ.get("ENGINE", Path.home() / "bsd" / "forgeflow"))
sys.path.insert(0, str(ENGINE))

from forgeflow import config, db, engine, queue  # noqa: E402

MANUAL = "clang/docs/BSC/BiShengCLanguageUserManual.md"
SEMA = "clang/lib/Sema/BSC/SemaBSC.cpp"


def sh(cwd, *a):
    subprocess.run(list(a), cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def blob(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          stdout=subprocess.PIPE).stdout.decode().strip()


def build_repo(work):
    repo = work / "repo"
    (repo / "clang/docs/BSC").mkdir(parents=True)
    (repo / "clang/lib/Sema/BSC").mkdir(parents=True)
    (repo / MANUAL).write_text("# BiSheng C Manual\nrule: freed once.\n")
    (repo / SEMA).write_text("// sema baseline\n")
    sh(repo, "git", "init", "-q")
    sh(repo, "git", "symbolic-ref", "HEAD", "refs/heads/main")
    sh(repo, "git", "config", "user.email", "d@d.invalid")
    sh(repo, "git", "config", "user.name", "d")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "base")
    pinned = blob(repo, "main:" + MANUAL)
    sh(repo, "git", "checkout", "-qb", "bsc-fix")
    # semantics change WITH a manual update (gate ok) + a FIXME (prescan hit)
    (repo / SEMA).write_text("// sema change\n// FIXME temporary hack\n")
    (repo / MANUAL).write_text("# BiSheng C Manual\nrule: freed once; "
                               "moved-from must not be freed again.\n")
    sh(repo, "git", "commit", "-aqm", "fix + manual")
    head = blob(repo, "bsc-fix")
    sh(repo, "git", "checkout", "-q", "main")
    return repo, pinned, head


def write_pack(work, repo, pinned, cli):
    notes = work / "code_notes"
    notes.mkdir()
    (HERE / "bsc" / "project.yaml").write_text("""\
name: bsc
paths: {{ repo: {repo}, code_notes: {notes} }}
tools: {{ git: {{ path: git }} }}
workflows: [workflows]
blocks:
  - ../review/blocks/reviewblocks.py
  - ../review/blocks/forge.py
  - ../review/blocks/providers.py
  - blocks/bsc.py
prompts: {{ review: prompts/review.md, refute: prompts/refute.md }}
schemas:
  review_findings:  ../review/schemas/review_findings.yaml
  refute_decisions: ../review/schemas/refute_decisions.yaml
agents:
  review: {{ backend: claude-cli, cli: {cli} }}
  refute: {{ backend: claude-cli, cli: {cli} }}
params:
  manual_path: {manual}
  manual_pinned_sha: {pinned}
  semantics_prefixes: [clang/lib/Sema/BSC]
  subsystem_map: {{}}
  prs_url: "http://unused/pulls"
  comment_url: "http://unused/pulls/{{payload.request.pr}}/comments"
  forge_auth: {{ token_ref: NONE }}
  deny_patterns: []
  min_severity: low
""".format(repo=repo, notes=notes, cli=cli, manual=MANUAL, pinned=pinned))
    return HERE / "bsc"


def seed_pattern(conn):
    conn.execute("INSERT INTO patterns(id, description, grep_rule, review_lens)"
                 " VALUES ('leftover-fixme', 'FIXME left in code', 'FIXME',"
                 " 'no FIXME/TODO in shipped code')")


def run(work, cli, head):
    pack = config.load_pack(write_pack(work, repo, pinned, cli))
    eng = engine.Engine(work / "ff", pack=pack)
    seed_pattern(eng.conn)
    queue.enqueue(eng.conn, "review",
                  {"branch": "bsc-fix", "base": "main", "pr": None,
                   "head_sha": head})
    eng.run_until_idle()
    return eng


if __name__ == "__main__":
    work = HERE / "demo-bsc-probe"
    shutil.rmtree(str(work), ignore_errors=True)
    work.mkdir()
    repo, pinned, head = build_repo(work)

    # ---- Scenario A: AI works -------------------------------------------
    print("=== A. AI works ===")
    eng = run(work, HERE / "scripts" / "fake_review_agent.py", head)
    task = eng.conn.execute("SELECT state FROM tasks WHERE kind='review'").fetchone()
    steps = [r["step"] for r in eng.conn.execute(
        "SELECT step FROM task_steps WHERE task_id=1 ORDER BY rowid")]
    findings = {r["key"]: r["state"] for r in eng.conn.execute(
        "SELECT key, state FROM findings")}
    lens_prompt = (work / "ff" / "data" / "runs" / "1" / "ask0" / "prompt").read_text()
    print("review task:", task["state"])
    print("steps:", " -> ".join(steps))
    print("findings:", json.dumps(findings, indent=1))
    print("manual context present:", "## context: bsc_manual" in lens_prompt,
          "| status CHANGED:", '"status":"CHANGED"' in lens_prompt.replace(" ", ""))
    def has(prefix, state):
        return any(k.startswith(prefix) and s == state
                   for k, s in findings.items())
    assert task["state"] == "done"
    assert "gate" in steps and "refute" in steps
    assert findings.get("review-bsc-fix-0") == "triaged"     # confirmed
    assert findings.get("review-bsc-fix-1") == "rejected"    # refuted
    assert has("pattern-bsc-fix-leftover-fixme", "triaged")  # machine finding
    assert "## context: bsc_manual" in lens_prompt

    # ---- Scenario B: AI down -> queue for another run -------------------
    print("\n=== B. AI down (backend broken) ===")
    dead = work / "dead.py"
    dead.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    dead.chmod(0o755)
    work2 = work / "b"
    work2.mkdir()
    eng2 = run(work2, dead, head)
    t = eng2.conn.execute("SELECT state, error_class FROM tasks"
                          " WHERE kind='review'").fetchone()
    f = {r["key"]: r["state"] for r in eng2.conn.execute(
        "SELECT key, state FROM findings")}
    print("review task:", dict(t))
    print("findings:", json.dumps(f, indent=1))
    assert t["state"] == "parked", dict(t)          # queued for another run
    assert not any(k.startswith("review-") for k in f)   # no unvetted AI findings
    assert any(k.startswith("pattern-bsc-fix-leftover-fixme") and s == "found"
               for k, s in f.items())               # machine finding filed, NOT posted

    # unpark -> it re-runs (still down here, re-parks; with GLM up it completes)
    n = queue.unpark(eng2.conn)
    print("unpark re-queued:", n, "task(s) — next run retries the AI")
    assert n == 1

    print("\nBSC PROBE: OK")
    print("  works: gate+prescan+lens+refute+adjudicate, findings vetted,"
          " manual authoritative")
    print("  AI down: task PARKED (re-queued by unpark), nothing posted,"
          " machine finding filed — never machine-only degraded")
