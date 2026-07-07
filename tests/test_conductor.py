from __future__ import annotations

import unittest

from helpers import PACKS, tmpdir

from forgeflow import db
from forgeflow.blocks import load_files, get
from forgeflow.util import tx

load_files([str(PACKS / "packs" / "bsc" / "blocks" / "conductor.py")])

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

    def test_bandit_tries_untried_first_then_by_index(self):
        from forgeflow.contract import CONTEXT_PROVIDERS
        from types import SimpleNamespace
        env = SimpleNamespace(conn=self.conn)
        # both untried -> lowest id first (inf ties by id)
        m = CONTEXT_PROVIDERS["hunt_method"](env, {"id": 1, "payload": {}}, {})
        self.assertEqual(m["method"], "m1")
        # give m1 trials with no yield, m2 still untried -> m2 (inf) wins
        self.conn.execute("UPDATE methods SET trials=5 WHERE id='m1'")
        m = CONTEXT_PROVIDERS["hunt_method"](env, {"id": 1, "payload": {}}, {})
        self.assertEqual(m["method"], "m2")


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
    (pack / "project.yaml").write_text("""\
name: bsc
paths: {{ repo: {base}, code_notes: {base}/notes }}
tools: {{ git: {{ path: git }} }}
workflows: [{bsc}/workflows]
blocks:
  - {rev}/blocks/reviewblocks.py
  - {rev}/blocks/forge.py
  - {rev}/blocks/providers.py
  - {rev}/blocks/hunt.py
  - {bsc}/blocks/bsc.py
  - {bsc}/blocks/conductor.py
prompts:
  review: {bsc}/prompts/review.md
  refute: {bsc}/prompts/refute.md
  explore: {bsc}/prompts/explorer.md
  exploit: {bsc}/prompts/exploiter.md
schemas:
  review_findings:  {rev}/schemas/review_findings.yaml
  refute_decisions: {rev}/schemas/refute_decisions.yaml
  explore_result:   {bsc}/schemas/explore_result.yaml
  exploit_result:   {bsc}/schemas/exploit_result.yaml
agents:
  review:  {{ backend: claude-cli, cli: {fa} }}
  refute:  {{ backend: claude-cli, cli: {fa} }}
  explore: {{ backend: claude-cli, cli: {ex} }}
  exploit: {{ backend: claude-cli, cli: {fa} }}
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
  hunt_regions: [ra, rb]
  hunt_methods: [{{ id: invariant-probe, description: p }}]
""".format(base=base, bsc=bsc, rev=rev, fa=FAKE_AGENT, ex=explorer_cli,
           clang=fake_clang))
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
