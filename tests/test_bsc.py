from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

from helpers import (FAKE_AGENT, PACKS, blob, dead_cli, git, make_engine, tmpdir)

from forgeflow import db, queue

from forgeflow.blocks import load_files, run_isolated  # noqa: E402
from forgeflow.contract import CONTEXT_PROVIDERS  # noqa: E402

# register via the engine's idempotent loader (same path the pack uses), so
# the provider/block aren't registered twice when an engine loads the pack
load_files([str(PACKS / "packs" / "bsc" / "blocks" / "bsc.py")])

MANUAL = "clang/docs/BSC/BiShengCLanguageUserManual.md"
SEMA = "clang/lib/Sema/BSC/SemaBSC.cpp"


def build_repo(base):
    repo = base / "repo"
    (repo / "clang/docs/BSC").mkdir(parents=True)
    (repo / "clang/lib/Sema/BSC").mkdir(parents=True)
    (repo / MANUAL).write_text("# Manual\nfreed once.\n")
    (repo / SEMA).write_text("// sema\n")
    git(repo, "init", "-q")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "config", "user.email", "d@d.invalid")
    git(repo, "config", "user.name", "d")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    pinned = blob(repo, "main:" + MANUAL)
    # branch: semantics change + manual update + a FIXME (prescan hit)
    git(repo, "checkout", "-qb", "bsc-fix")
    (repo / SEMA).write_text("// sema change\n// FIXME hack\n")
    (repo / MANUAL).write_text("# Manual\nfreed once; moved-from not freed again.\n")
    git(repo, "commit", "-aqm", "fix + manual")
    head = blob(repo, "bsc-fix")
    git(repo, "checkout", "-q", "main")
    return repo, pinned, head


class BscGroundTruthTest(unittest.TestCase):
    """The deterministic rules: manual-wins-on-change and the must-update gate."""

    def setUp(self):
        self.base = tmpdir()
        self.repo, self.pinned, self.head = build_repo(self.base)

    def _env(self):
        pack = SimpleNamespace(
            paths={"repo": str(self.repo)},
            params={"manual_path": MANUAL, "manual_pinned_sha": self.pinned},
            tools={})
        return SimpleNamespace(pack=pack, data_dir=str(self.base / "data"))

    def test_manual_unchanged_trusts_skills(self):
        r = CONTEXT_PROVIDERS["bsc_manual"](
            self._env(), {"payload": {"branch": "main"}}, {})
        self.assertEqual(r["status"], "current")
        self.assertTrue(r["authoritative"])

    def test_manual_changed_overrides_skills(self):
        r = CONTEXT_PROVIDERS["bsc_manual"](
            self._env(), {"payload": {"branch": "bsc-fix"}}, {})
        self.assertEqual(r["status"], "CHANGED")
        self.assertIn("Manual", r["toc"])          # TOC (headings), not cover page

    def test_gate_semantics_without_manual_flags(self):
        # a branch that touches semantics but NOT the manual
        git(self.repo, "checkout", "-qb", "bad")
        (self.repo / SEMA).write_text("// silent change\n")
        git(self.repo, "commit", "-aqm", "semantics only")
        git(self.repo, "checkout", "-q", "main")
        out, res = run_isolated(
            "bsc.manual_gate",
            {"repo": str(self.repo), "manual_path": MANUAL,
             "semantics_prefixes": ["clang/lib/Sema/BSC"], "_tools": {}},
            task={"id": 1, "attempts": 0,
                  "payload": {"base": "main", "branch": "bad"}},
            prev={"path": "/ws"})
        self.assertEqual(out, "flagged")
        self.assertEqual(res["_staged"][0]["key"], "pattern-bad-manual-not-updated")

    def test_gate_semantics_with_manual_ok(self):
        out, res = run_isolated(
            "bsc.manual_gate",
            {"repo": str(self.repo), "manual_path": MANUAL,
             "semantics_prefixes": ["clang/lib/Sema/BSC"], "_tools": {}},
            task={"id": 1, "attempts": 0,
                  "payload": {"base": "main", "branch": "bsc-fix"}},
            prev={"path": "/ws"})
        self.assertEqual(out, "ok")
        self.assertNotIn("_staged", res)


def write_pack(base, repo, pinned, cli):
    notes = base / "notes"
    notes.mkdir()
    probes = base / "probes"
    probes.mkdir()          # empty -> sweep returns 'error' -> routes to lens
    pack = base / "pack"
    pack.mkdir()
    (pack / "project.yaml").write_text("""\
name: bsc
paths: {{ repo: {repo}, code_notes: {notes} }}
tools: {{ git: {{ path: git }} }}
workflows: [{bsc}/workflows]
blocks:
  - {rev}/blocks/reviewblocks.py
  - {rev}/blocks/forge.py
  - {rev}/blocks/providers.py
  - {rev}/blocks/hunt.py
  - {bsc}/blocks/bsc.py
prompts: {{ review: {bsc}/prompts/review.md, refute: {bsc}/prompts/refute.md }}
schemas:
  review_findings:  {rev}/schemas/review_findings.yaml
  refute_decisions: {rev}/schemas/refute_decisions.yaml
agents:
  review: {{ backend: claude-cli, cli: {cli} }}
  refute: {{ backend: claude-cli, cli: {cli} }}
params:
  manual_path: {manual}
  manual_pinned_sha: {pinned}
  semantics_prefixes: [clang/lib/Sema/BSC]
  build_cmd: ["true"]
  probes_dir: {probes}
  probe_cmd: ["true", "{{probe}}"]
  probe_workers: 2
  prs_url: "http://unused/pulls"
  comment_url: "http://unused/pulls/{{payload.request.pr}}/comments"
  forge_auth: {{ token_ref: NONE }}
  deny_patterns: []
  min_severity: low
""".format(repo=repo, notes=notes, probes=probes, bsc=(PACKS / "packs" / "bsc"),
           rev=(PACKS / "packs" / "review"), cli=cli, manual=MANUAL, pinned=pinned))
    return pack


class BscAiMandatoryTest(unittest.TestCase):
    """BSC must be AI-reviewed: works end to end; if the AI breaks down the
    review PARKS (re-queued by unpark), never machine-only degraded."""

    def setUp(self):
        self.base = tmpdir()
        self.repo, self.pinned, self.head = build_repo(self.base)

    def _eng(self, cli):
        return make_engine(self.base / ("ff-%s" % id(cli)),
                           write_pack(self.base, self.repo, self.pinned, cli))

    def _run(self, eng):
        queue.enqueue(eng.conn, "review",
                      {"branch": "bsc-fix", "base": "main", "pr": None,
                       "head_sha": self.head})
        eng.run_until_idle()

    def test_ai_works_full_pipeline(self):
        eng = self._eng(FAKE_AGENT)
        self._run(eng)
        t = eng.conn.execute("SELECT state FROM tasks WHERE kind='review'").fetchone()
        self.assertEqual(t["state"], "done")
        # AI-only pipeline: no prescan / no machine code-review step
        steps = [r["step"] for r in eng.conn.execute(
            "SELECT step FROM task_steps WHERE task_id=1 ORDER BY rowid")]
        self.assertEqual(steps, ["workspace", "diff", "gate", "sweep_base",
                                 "build", "sweep", "lens", "file", "refute",
                                 "adjudicate", "announce"])
        f = {r["key"]: r["state"] for r in eng.conn.execute(
            "SELECT key, state FROM findings")}
        # every finding came from the AI (review-*), vetted by refutation
        self.assertEqual(f.get("review-bsc-fix-0"), "triaged")
        self.assertEqual(f.get("review-bsc-fix-1"), "rejected")
        self.assertFalse(any(k.startswith("pattern-") for k in f))  # no no-AI findings
        prompt = (list((self.base).glob("ff-*/data/runs/1/ask0/prompt"))[0]).read_text()
        self.assertIn("## context: bsc_manual", prompt)

    def test_ai_down_parks_with_nothing(self):
        cli = dead_cli(self.base / "dead.py")
        eng = self._eng(cli)
        self._run(eng)
        t = eng.conn.execute("SELECT state, error_class FROM tasks"
                             " WHERE kind='review'").fetchone()
        self.assertEqual(t["state"], "parked")            # queued for another run
        # AI-only: with the model down there is NO output at all — no
        # machine findings, nothing posted. The review must have AI.
        n = eng.conn.execute("SELECT count(*) c FROM findings").fetchone()["c"]
        self.assertEqual(n, 0)
        self.assertEqual(queue.unpark(eng.conn), 1)       # unpark re-queues it


if __name__ == "__main__":
    unittest.main()
