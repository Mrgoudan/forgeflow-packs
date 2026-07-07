from __future__ import annotations

import json
import os
import unittest

from helpers import (FAKE_AGENT, FakeForge, PACKS, dead_cli, git, make_engine,
                     tmpdir)

from forgeflow import db, queue


def build_repo(base):
    repo = base / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "config", "user.email", "d@d.invalid")
    git(repo, "config", "user.name", "d")
    (repo / "store.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "feature-discount")
    (repo / "discount.py").write_text(
        "import pickle\n\n\ndef load(b):\n    return pickle.loads(b)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature")
    head = __import__("helpers").blob(repo, "feature-discount")
    git(repo, "checkout", "-q", "main")
    return repo, head


def write_pack(base, repo, forge_url, cli, min_sev="medium"):
    pack = base / "pack"
    pack.mkdir()
    (pack / "project.yaml").write_text("""\
name: review
paths: {{ repo: {repo} }}
tools: {{ git: {{ path: git }} }}
workflows: [{src}/workflows]
blocks:
  - {src}/blocks/reviewblocks.py
  - {src}/blocks/forge.py
  - {src}/blocks/providers.py
prompts: {{ review: {src}/prompts/review.md, refute: {src}/prompts/refute.md }}
schemas:
  review_findings:  {src}/schemas/review_findings.yaml
  refute_decisions: {src}/schemas/refute_decisions.yaml
agents:
  review: {{ backend: claude-cli, cli: {cli} }}
  refute: {{ backend: claude-cli, cli: {cli} }}
params:
  prs_url: "{forge}/pulls?state=open"
  comment_url: "{forge}/pulls/{{payload.request.pr}}/comments"
  forge_auth: {{ token_ref: T, style: query, name: access_token }}
  deny_patterns: []
  min_severity: {sev}
""".format(repo=repo, src=(PACKS / "packs" / "review"), cli=cli, forge=forge_url, sev=min_sev))
    return pack


class ReviewPipelineTest(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        self.repo, self.head = build_repo(self.base)
        secrets = self.base / "secrets.env"
        secrets.write_text("FORGE_TOKEN_T=tok-1\n")
        secrets.chmod(0o600)
        os.environ["FORGEFLOW_SECRETS"] = str(secrets)
        os.environ["FORGE_WRITE"] = "1"
        self.forge = FakeForge(7, self.head)
        self.addCleanup(self.forge.stop)
        self.addCleanup(lambda: os.environ.pop("FORGE_WRITE", None))

    def _eng(self, cli=FAKE_AGENT, min_sev="medium"):
        pack = write_pack(self.base, self.repo, self.forge.url, cli, min_sev)
        return make_engine(self.base / "ff", pack)

    def test_full_chain_with_refutation_and_gate(self):
        eng = self._eng()
        eng.conn.execute(
            "INSERT INTO patterns(id, description, grep_rule, review_lens)"
            " VALUES ('unsafe','x','pickle\\.loads','watch deserialization')")
        # prior finding in the touched file (history context)
        oid = eng.conn.execute(
            "INSERT INTO code_objects(repo,path,kind,first_seen_sha,last_seen_sha)"
            " VALUES ('demo','discount.py','file','s','s')").lastrowid
        fid = db.upsert_finding(eng.conn, "F-old", "old deser bug", "bughunt", "demo")
        eng.conn.execute("INSERT INTO implications(finding_id,object_id,role)"
                         " VALUES (?,?,'root_cause')", (fid, oid))

        db.emit_event(eng.conn, "review.requested",
                      {"branch": "feature-discount", "base": "main", "pr": 7,
                       "head_sha": self.head}, eng.subscriptions)
        eng.run_until_idle()

        f = {r["key"]: r["state"] for r in eng.conn.execute(
            "SELECT key, state FROM findings")}
        # refutation: RCE confirmed, div-zero rejected
        self.assertEqual(f.get("review-feature-discount-0"), "triaged")
        self.assertEqual(f.get("review-feature-discount-1"), "rejected")
        # machine finding triaged
        self.assertTrue(any(k.startswith("pattern-feature-discount-unsafe")
                            and s == "triaged" for k, s in f.items()))
        # posted comment holds only confirmed, and carried query auth
        self.assertEqual(len(self.forge.comments), 1)
        body = self.forge.comments[0]["body"]
        self.assertIn("RCE", body)
        self.assertNotIn("divide by zero", body)
        self.assertIn("tok-1", self.forge.comments[0]["path"])
        # lens prompt pinned with history
        prompt = (self.base / "ff" / "data" / "runs" / "1" / "ask0" / "prompt").read_text()
        self.assertIn("## context: history", prompt)
        self.assertIn("F-old", prompt)

    def test_clean_review_still_posts_no_defects(self):
        # always post something: a CLEAN review posts a "no defects" comment
        eng = self._eng()
        db.emit_event(eng.conn, "review.requested",
                      {"branch": "feature-discount", "base": "main", "pr": 7,
                       "head_sha": self.head, "_test_clean": True},
                      eng.subscriptions)
        eng.run_until_idle()
        self.assertEqual(len(self.forge.comments), 1)         # posted, not skipped
        self.assertIn("no defects found", self.forge.comments[0]["body"])
        n = eng.conn.execute("SELECT count(*) c FROM findings").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_degraded_backend_down_parks(self):
        cli = dead_cli(self.base / "dead.py")
        eng = self._eng(cli=cli)
        eng.conn.execute(
            "INSERT INTO patterns(id, description, grep_rule) VALUES"
            " ('unsafe','x','pickle\\.loads')")
        db.emit_event(eng.conn, "review.requested",
                      {"branch": "feature-discount", "base": "main", "pr": None},
                      eng.subscriptions)
        eng.run_until_idle()
        t = eng.conn.execute("SELECT state FROM tasks WHERE kind='review'").fetchone()
        self.assertEqual(t["state"], "parked")     # AI down -> park
        # no-AI core still filed its machine finding
        machine = eng.conn.execute(
            "SELECT state FROM findings WHERE key LIKE 'pattern-%'").fetchone()
        self.assertIsNotNone(machine)


if __name__ == "__main__":
    unittest.main()
