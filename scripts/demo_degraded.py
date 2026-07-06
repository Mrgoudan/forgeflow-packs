#!/usr/bin/env python3
"""Degraded-mode proof: the review agent is COMPLETELY down (CLI always
exits nonzero), yet the review still produces value — the no-AI pattern
scan files a machine finding and adjudicate triages it. AI stages park
for later retry; the system is never down, only its yield is reduced.

Usage: ENGINE=~/bsd/forgeflow python3 scripts/demo_degraded.py
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


def sh(cwd, *a):
    subprocess.run(list(a), cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    work = HERE / "demo-degraded-run"
    shutil.rmtree(str(work), ignore_errors=True)
    work.mkdir()

    repo = work / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q")
    sh(repo, "git", "symbolic-ref", "HEAD", "refs/heads/main")
    sh(repo, "git", "config", "user.email", "d@d.invalid")
    sh(repo, "git", "config", "user.name", "d")
    (repo / "seed.py").write_text("x = 1\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "base")
    sh(repo, "git", "checkout", "-qb", "feature")
    (repo / "loader.py").write_text("import pickle\n\n\n"
                                    "def load(b):\n    return pickle.loads(b)\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "feature")
    sh(repo, "git", "checkout", "-q", "main")

    # a CLI that ALWAYS fails: the model is unreachable
    dead = work / "dead_cli.py"
    dead.write_text("#!/usr/bin/env python3\nimport sys\n"
                    "sys.stderr.write('backend down\\n')\nsys.exit(1)\n")
    dead.chmod(0o755)

    (HERE / "review" / "project.yaml").write_text("""\
name: review
paths: {{ repo: {repo} }}
tools: {{ git: {{ path: git }} }}
workflows: [workflows]
blocks:    [blocks/reviewblocks.py, blocks/forge.py, blocks/providers.py]
prompts: {{ review: prompts/review.md, refute: prompts/refute.md }}
schemas:
  review_findings: schemas/review_findings.yaml
  refute_decisions: schemas/refute_decisions.yaml
agents:
  review: {{ backend: claude-cli, cli: {cli} }}
  refute: {{ backend: claude-cli, cli: {cli} }}
params:
  min_severity: low
  prs_url: "http://unused/pulls"
  comment_url: "http://unused/pulls/{{payload.request.pr}}/comments"
  forge_auth: {{ token_ref: NONE }}
  deny_patterns: []
""".format(repo=repo, cli=dead))

    eng = engine.Engine(work / "ff", pack=config.load_pack(HERE / "review"))
    eng.conn.execute(
        "INSERT INTO patterns(id, description, grep_rule, review_lens)"
        " VALUES ('unsafe-deserialize', 'external deserialization',"
        " 'pickle\\.loads', 'watch deserialization')")

    queue.enqueue(eng.conn, "review",
                  {"branch": "feature", "base": "main", "pr": None})
    eng.run_until_idle()

    task = eng.conn.execute("SELECT state, error_class FROM tasks"
                            " WHERE kind='review'").fetchone()
    print("review task:", dict(task))
    print("findings:")
    for f in eng.conn.execute("SELECT key, state, severity FROM findings"):
        print(" ", dict(f))
    runs = eng.conn.execute(
        "SELECT id, exit_code, verdict FROM runs").fetchall()
    print("agent runs (all failed, pinned as evidence):",
          [dict(r) for r in runs])

    # the machine finding was filed BEFORE the agent was even reached...
    machine = eng.conn.execute(
        "SELECT state FROM findings WHERE key LIKE 'pattern-feature-%'"
    ).fetchone()
    assert machine is not None, "no-AI core produced nothing"
    # ...and the task PARKED on the dead agent (resumable), not failed/hung
    assert task["state"] == "parked", dict(task)
    print("\nDEGRADED MODE: agent down -> task PARKED (resumable);"
          " no-AI pattern finding still FILED. System never down.")


if __name__ == "__main__":
    main()
