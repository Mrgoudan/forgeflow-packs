from __future__ import annotations

import unittest

from helpers import PACKS, tmpdir

from forgeflow import db
from forgeflow.blocks import load_files, get
from forgeflow.util import tx

load_files([str(PACKS / "packs" / "hunt" / "blocks" / "conductor.py")])

REGIONS = [{"id": "a"}, {"id": "b"}, {"id": "c"}]  # seed via list of ids below
METHODS = [{"id": "m1", "description": "d1"}, {"id": "m2", "description": "d2"}]


def _ctx(conn, **kw):
    kw.setdefault("_conn", conn)
    kw.setdefault("_timeout_s", 30)
    kw.setdefault("_step_dir", str(tmpdir()))
    kw.setdefault("_tools", {})
    return kw


def _task(conn, tid):
    """A real tasks row so the regions.leased_by_task FK resolves."""
    conn.execute("INSERT OR IGNORE INTO tasks(id, kind, payload, payload_hash)"
                 " VALUES (?, 'hunt_explore', '{}', ?)", (tid, "h%d" % tid))
    return {"id": tid, "attempts": 0, "payload": {}}


class ConductorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.conn = db.connect(self.dir / "t.db")
        with tx(self.conn):
            get("hunt.seed").fn(_ctx(self.conn, repo="r",
                                     regions=["a", "b", "c"], methods=METHODS),
                                {"id": 0, "attempts": 0, "payload": {}}, {})

    def _pick(self, task_id):
        with tx(self.conn):
            return get("hunt.pick_region").fn(
                _ctx(self.conn, max_explorers=6), _task(self.conn, task_id), {})

    def _merge(self, task_id, verified):
        with tx(self.conn):
            return get("hunt.merge_explore").fn(
                _ctx(self.conn, repo="r"), _task(self.conn, task_id),
                {"verified": verified})

    def test_seed_idempotent(self):
        self.assertEqual(self.conn.execute("SELECT count(*) c FROM regions").fetchone()["c"], 3)
        with tx(self.conn):
            get("hunt.seed").fn(_ctx(self.conn, repo="r", regions=["a", "b", "c"],
                                     methods=METHODS),
                                {"id": 0, "attempts": 0, "payload": {}}, {})
        self.assertEqual(self.conn.execute("SELECT count(*) c FROM regions").fetchone()["c"], 3)

    def test_seed_grep_discovers_guarded_files(self):
        # the surface includes generic files that BRANCH on the feature guard,
        # not just the whole-feature dirs.
        d = tmpdir()
        repo = d / "repo"
        (repo / "clang/lib/Parse").mkdir(parents=True)
        (repo / "clang/lib/Parse/ParseExpr.cpp").write_text(
            "void f(){ if (getLangOpts().BSC) {} }\n")
        (repo / "clang/lib/Parse/ParseOther.cpp").write_text("// nothing here\n")
        conn = db.connect(d / "g.db")
        with tx(conn):
            get("hunt.seed").fn(
                _ctx(conn, repo=str(repo), regions=[],
                     region_grep=r"getLangOpts\(\)\.BSC", region_scan=["clang"]),
                {"id": 0, "attempts": 0, "payload": {}}, {})
        regions = [r["id"] for r in conn.execute("SELECT id FROM regions")]
        self.assertIn("clang/lib/Parse/ParseExpr.cpp", regions)   # guarded -> in
        self.assertNotIn("clang/lib/Parse/ParseOther.cpp", regions)  # unguarded -> out

    def test_pick_region_deterministic_and_leases(self):
        o, r = self._pick(1)
        self.assertEqual((o, r["region"]), ("leased", "a"))   # first by id
        o, r = self._pick(2)
        self.assertEqual(r["region"], "b")                    # a is leased -> b
        # a is leased to task 1
        self.assertEqual(self.conn.execute(
            "SELECT leased_by_task FROM regions WHERE id='a'").fetchone()[0], 1)

    def test_cap_enforced(self):
        self.conn.execute("INSERT INTO regions(id, repo) VALUES ('d','r')")  # 4 regions
        for i in range(3):
            self._pick(i + 1)                                 # lease a,b,c (d free)
        # cap=3: 3 leased hits the cap -> saturated even though d is free
        with tx(self.conn):
            o, _ = get("hunt.pick_region").fn(_ctx(self.conn, max_explorers=3),
                                              _task(self.conn, 10), {})
        self.assertEqual(o, "saturated")
        # cap=6: 3 < 6 -> leases the free region d
        with tx(self.conn):
            o, r = get("hunt.pick_region").fn(_ctx(self.conn, max_explorers=6),
                                              _task(self.conn, 11), {})
        self.assertEqual((o, r["region"]), ("leased", "d"))

    def test_confirm_files_finding_pattern_and_emits(self):
        self._pick(1)                                          # lease a to task 1
        o, res = self._merge(1, {"confirmed": True, "key": "BUG-1",
                                 "title": "boom", "pattern": "cls-x",
                                 "severity": "high", "grep_rule": "boom"})
        self.assertEqual(o, "confirmed")
        finds = [s for s in res["_staged"] if s["op"] == "upsert_finding"]
        emits = [s["name"] for s in res["_staged"] if s["op"] == "emit_event"]
        self.assertEqual(finds[0]["key"], "BUG-1")
        self.assertEqual(finds[0]["source"], "bughunt")
        self.assertIn("hunt.pattern_confirmed", emits)        # spawn exploiter
        self.assertIn("hunt.explore_requested", emits)        # auto-swap
        # pattern row created, region lease released + streak reset, method credited
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM patterns WHERE id='cls-x'").fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT leased_by_task FROM regions WHERE id='a'").fetchone()[0])
        self.assertEqual(self.conn.execute(
            "SELECT sum(verified_yield) s FROM methods").fetchone()["s"], 1)

    def test_dry_streak_cooldown_and_saturation(self):
        # drive every region dry 3x -> all cool -> campaign saturates
        explores = 0
        for i in range(40):
            tid = 100 + i                       # same task id for pick + merge
            o, r = self._pick(tid)
            if o == "saturated":
                break
            explores += 1
            self._merge(tid, {"confirmed": False})
        # all three regions cooled
        cooled = self.conn.execute(
            "SELECT count(*) c FROM regions WHERE cooldown_until_round IS NOT NULL").fetchone()["c"]
        self.assertEqual(cooled, 3)
        # dispatch eventually reports saturated (bounded, not infinite)
        with tx(self.conn):
            o, r = get("hunt.pick_region").fn(_ctx(self.conn, max_explorers=6),
                                              {"id": 999, "attempts": 0, "payload": {}}, {})
        self.assertEqual(o, "saturated")

    def test_reading_loop_add_update_no_churn(self):
        # the exploration->readings loop: new note writes, corrected note
        # updates in place, identical re-read is a no-op (governance: one
        # reading per (file, content), latest understanding wins).
        rec = get("hunt.merge_explore").fn.__globals__["_record_reading"]
        n1 = {"object": "fn", "invariant": "inv one", "candidates": ["a"]}
        self.assertTrue(rec(self.conn, "r", "a", n1))            # new -> written
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM readings").fetchone()["c"], 1)
        self.assertFalse(rec(self.conn, "r", "a", n1))           # identical -> no churn
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM readings").fetchone()["c"], 1)
        n2 = {"object": "fn", "invariant": "inv TWO corrected", "candidates": ["a"]}
        self.assertTrue(rec(self.conn, "r", "a", n2))            # changed -> update
        rows = self.conn.execute("SELECT summary FROM readings").fetchall()
        self.assertEqual(len(rows), 1)                           # still one row
        self.assertIn("inv TWO corrected", rows[0]["summary"])

    def test_method_dispatch_counts_and_diversifies(self):
        # dispatch assigns an arm, counts the pull immediately, and records it
        # on the task so the NEXT serialized explorer diverges (no monoculture).
        _o, r1 = self._pick(1)
        self.assertEqual(r1["method"], "m1")             # both untried -> lowest id
        self.assertEqual(self.conn.execute(
            "SELECT trials FROM methods WHERE id='m1'").fetchone()[0], 1)
        _o, r2 = self._pick(2)
        self.assertEqual(r2["method"], "m2")             # m1 counted -> untried m2 wins
        # arm recorded on each task -> merge credits exactly what was used
        self.assertEqual(self.conn.execute(
            "SELECT json_extract(payload,'$.method') FROM tasks WHERE id=1"
        ).fetchone()[0], "m1")

    def _scout(self, prev):
        with tx(self.conn):
            return get("hunt.merge_scout").fn(_ctx(self.conn, repo="r"),
                                              _task(self.conn, 1), prev)

    def test_scout_proposes_new_methods_and_reopens(self):
        # cool a region so we can see the reopen clear it
        self.conn.execute("UPDATE regions SET dry_streak=2, cooldown_until_round=99"
                          " WHERE id='a'")
        o, r = self._scout({"verdict": "PROPOSED", "methods": [
            {"id": "new-lens", "description": "a fresh tactic"},
            {"id": "m1", "description": "dup — already on the bench"}]})
        self.assertEqual((o, r["added"]), ("proposed", 1))       # dup skipped
        self.assertEqual(self.conn.execute(
            "SELECT status FROM methods WHERE id='new-lens'").fetchone()[0], "active")
        # region cooldowns cleared -> the new tactic gets a full surface
        row = self.conn.execute("SELECT dry_streak, cooldown_until_round"
                                " FROM regions WHERE id='a'").fetchone()
        self.assertEqual((row[0], row[1]), (0, None))
        self.assertIn("hunt.explore_requested",
                      [s["name"] for s in r["_staged"]])

    def test_scout_no_new_method_ends_campaign(self):
        o, r = self._scout({"verdict": "NO_NEW_METHOD"})
        self.assertEqual((o, r["added"]), ("saturated", 0))
        self.assertNotIn("_staged", r)                          # nothing reopened

    def test_spent_arm_retired_after_trial_budget(self):
        _o, r = self._pick(1)                                   # dispatch m1
        m = r["method"]
        self.conn.execute("UPDATE methods SET trials=8 WHERE id=?",
                          (m,))                                  # simulate a spent budget
        self._merge(1, {"confirmed": False})                    # dry -> retire it
        self.assertEqual(self.conn.execute(
            "SELECT status FROM methods WHERE id=?", (m,)).fetchone()[0], "exhausted")

    def test_method_provider_reads_dispatched_arm(self):
        # the provider surfaces the arm dispatch recorded, it does NOT re-pick
        from forgeflow.contract import CONTEXT_PROVIDERS
        from types import SimpleNamespace
        self._pick(1)                                    # dispatch m1 to task 1
        env = SimpleNamespace(conn=self.conn)
        m = CONTEXT_PROVIDERS["hunt_method"](env, {"id": 1, "payload": {}}, {})
        self.assertEqual(m["method"], "m1")


if __name__ == "__main__":
    unittest.main()


import os
import subprocess
from helpers import make_engine, FAKE_AGENT
from forgeflow import db as _db, loader as _loader, config as _cfg


def _hunt_pack(base, explorer_cli):
    """A bsc-shaped pack whose explore agent is the fake explorer and whose
    hunt_clang exits 0 (so verify finds nothing to confirm)."""
    (base / "notes").mkdir(); (base / "probes").mkdir()
    fake_clang = base / "clang.sh"
    fake_clang.write_text("#!/bin/sh\nexit 0\n"); fake_clang.chmod(0o755)
    pack = base / "pack"; pack.mkdir()
    bsc = PACKS / "packs" / "bsc"; rev = PACKS / "packs" / "review"
    hunt = PACKS / "packs" / "hunt"
    fix = PACKS / "packs" / "fix"
    sc = PACKS / "tests" / "fixtures" / "fake_scout.py"
    (pack / "project.yaml").write_text("""\
name: bsc
paths: {{ repo: {base}, code_notes: {base}/notes }}
tools: {{ git: {{ path: git }} }}
workflows: [{bsc}/workflows]
blocks:
  - {rev}/blocks/reviewblocks.py
  - {rev}/blocks/forge.py
  - {rev}/blocks/providers.py
  - {hunt}/blocks/probe.py
  - {bsc}/blocks/bsc.py
  - {bsc}/blocks/seed.py
  - {hunt}/blocks/conductor.py
  - {fix}/blocks/fix.py
prompts:
  review: {bsc}/prompts/review.md
  refute: {bsc}/prompts/refute.md
  explore: {bsc}/prompts/explorer.md
  exploit: {bsc}/prompts/exploiter.md
  scout:  {bsc}/prompts/scout.md
  fix:    {bsc}/prompts/fixer.md
schemas:
  review_findings:  {rev}/schemas/review_findings.yaml
  refute_decisions: {rev}/schemas/refute_decisions.yaml
  explore_result:   {hunt}/schemas/explore_result.yaml
  exploit_result:   {hunt}/schemas/exploit_result.yaml
  scout_result:     {hunt}/schemas/scout_result.yaml
  fix_patch:        {fix}/schemas/fix_patch.yaml
agents:
  review:  {{ backend: claude-cli, cli: {fa} }}
  refute:  {{ backend: claude-cli, cli: {fa} }}
  explore: {{ backend: claude-cli, cli: {ex} }}
  exploit: {{ backend: claude-cli, cli: {fa} }}
  scout:   {{ backend: claude-cli, cli: {sc} }}
  fix:     {{ backend: claude-cli, cli: {fa} }}
params:
  manual_path: m.md
  semantics_prefixes: [x]
  build_cmd: ["true"]
  refresh_cmd: ["true"]
  baseline_root: {base}/bl
  probes_dir: {base}/probes
  probe_cmd: ["true", "{{probe}}"]
  probe_workers: 2
  prs_url: "http://x/pulls"
  comment_url: "http://x/pulls/{{payload.request.pr}}/comments"
  forge_auth: {{ token_ref: NONE }}
  deny_patterns: []
  min_severity: low
  hunt_max_explorers: 6
  hunt_clang: {clang}
  probe_include: {base}/probes
  vault: {base}/novault
  hunt_regions: [ra, rb]
  hunt_region_grep: ""
  hunt_region_scan: []
  fix_branch_prefix: "forgeflow/fix-"
  fix_build_cmd: ["true"]
  pr_create_url: "http://x/pulls"
  issue_url: "http://x/issues"
  issue_repo: "r"
  issue_comment_url: "http://x/issues"
  issue_title: "t"
""".format(base=base, bsc=bsc, rev=rev, hunt=hunt, fix=fix, fa=FAKE_AGENT,
           ex=explorer_cli, sc=sc, clang=fake_clang))
    return pack


class HuntLoopTest(unittest.TestCase):
    def test_dry_loop_saturates_and_terminates(self):
        base = tmpdir()
        cli = PACKS / "tests" / "fixtures" / "fake_explorer.py"
        eng = make_engine(base / "ff", _hunt_pack(base, cli))
        # kick the whole campaign: seed + build(true) + sweep(empty->error) + loop
        _db.emit_event(eng.conn, "hunt.round_requested", {"base": "main"},
                       eng.subscriptions)
        executed = eng.run_until_idle()
        # loop terminated (no pending/running left) — bounded, not infinite
        left = eng.conn.execute(
            "SELECT count(*) c FROM tasks WHERE state IN ('pending','running')"
        ).fetchone()["c"]
        self.assertEqual(left, 0)
        # both regions dried to cooldown (2 regions x 3 dries)
        cooled = eng.conn.execute(
            "SELECT count(*) c FROM regions WHERE cooldown_until_round IS NOT NULL"
        ).fetchone()["c"]
        self.assertEqual(cooled, 2)
        # explorers actually ran (>= 6 dry explores before saturation)
        n_expl = eng.conn.execute(
            "SELECT count(*) c FROM tasks WHERE kind='hunt_explore' AND state='done'"
        ).fetchone()["c"]
        self.assertGreaterEqual(n_expl, 6)
        # no unvetted findings from a dry campaign
        self.assertEqual(eng.conn.execute(
            "SELECT count(*) c FROM findings").fetchone()["c"], 0)
        # ...but exploration still accumulated function readings (the loop
        # runs on dry turns too), one per region explored
        self.assertGreaterEqual(eng.conn.execute(
            "SELECT count(*) c FROM readings").fetchone()["c"], 1)
