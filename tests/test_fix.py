from __future__ import annotations

import os
import subprocess
import unittest

from helpers import PACKS, git, tmpdir, ITEM_STATES, pack_db

from forgeflow import db
from forgeflow.blocks import load_files, get
from forgeflow.util import tx

load_files([str(PACKS / "packs" / "fix" / "blocks" / "fix.py"),
            str(PACKS / "packs" / "review" / "blocks" / "forge.py")])

_STEPS = {"triaged": ["triaged"], "fixing": ["triaged", "fixing"],
          "verifying": ["triaged", "fixing", "verifying"]}


def _ctx(conn, **kw):
    kw.setdefault("_conn", conn)
    kw.setdefault("_timeout_s", 60)
    kw.setdefault("_step_dir", str(tmpdir()))
    kw.setdefault("_tools", {})
    return kw


def _finding(conn, key="BUG-1", state="triaged", repo="r"):
    """A item driven (via the real transition fn) to `state`."""
    fid = db.upsert_item(conn, key, "boom in FooChecker", "bughunt", repo,
                            detail='{"probe":"x"}', severity="high", pattern="C1")
    for s in _STEPS[state]:
        db.record_transition(conn, fid, s, "test:setup", subscriptions={},
                     states=ITEM_STATES)
    return fid


def _task(key="BUG-1", base="main"):
    return {"id": 1, "attempts": 0, "payload": {"item": key, "base": base}}


class FixPrepareTest(unittest.TestCase):
    def setUp(self):
        self.conn = pack_db(tmpdir() / "t.db")

    def test_prepare_triaged_to_fixing_names_branch(self):
        with tx(self.conn):
            _finding(self.conn, state="triaged")
        with tx(self.conn):
            o, r = get("fix.prepare").fn(_ctx(self.conn, repo="r"), _task(), {})
        self.assertEqual(o, "prepared")
        self.assertTrue(r["branch"].endswith("BUG-1"))
        t = [s for s in r["_staged"] if s["op"] == "transition"][0]
        self.assertEqual(t["to_state"], "fixing")
        self.assertEqual(self.conn.execute(
            "SELECT branch FROM items WHERE key='BUG-1'").fetchone()[0], r["branch"])

    def test_prepare_skips_when_not_triaged(self):
        with tx(self.conn):
            _finding(self.conn, state="fixing")            # already fixing
        with tx(self.conn):
            o, _ = get("fix.prepare").fn(_ctx(self.conn, repo="r"), _task(), {})
        self.assertEqual(o, "skip")


class FixVerifyTest(unittest.TestCase):
    def setUp(self):
        d = tmpdir()
        self.repo = d / "repo"
        self.repo.mkdir()
        (self.repo / "foo.cpp").write_text("int x = 0;\n")
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "d@d.invalid")
        git(self.repo, "config", "user.name", "d")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        # a REAL diff (captured from git), reverted so verify re-applies it
        (self.repo / "foo.cpp").write_text("int x = 1; // fixed\n")
        self.patch = subprocess.run(["git", "-C", str(self.repo), "diff"],
                                    capture_output=True, text=True).stdout
        git(self.repo, "checkout", "--", "foo.cpp")
        self.conn = pack_db(d / "t.db")
        self.clang = d / "clang.sh"                        # accepts (exit 0)
        self.clang.write_text("#!/bin/sh\nexit 0\n")
        self.clang.chmod(0o755)

    def _verify(self, prev):
        with tx(self.conn):
            _finding(self.conn, state="fixing")
        with tx(self.conn):
            return get("fix.verify").fn(
                _ctx(self.conn, repo=str(self.repo), clang=str(self.clang),
                     build_cmd=["true"]), _task(), prev)

    def test_green_when_repro_now_correct(self):
        # patch applies, build ok, clang accepts, expect_error False -> correct
        o, r = self._verify({"patch": self.patch, "probe": "safe();",
                             "expect_error": False})
        self.assertEqual(o, "green")
        self.assertEqual([s for s in r["_staged"]
                          if s["op"] == "transition"][0]["to_state"], "verifying")

    def test_red_when_repro_still_wrong_resets_tree(self):
        # clang accepts but we EXPECTED a rejection -> still wrong -> red
        o, r = self._verify({"patch": self.patch, "probe": "unsafe();",
                             "expect_error": True})
        self.assertEqual(o, "red")
        self.assertEqual((self.repo / "foo.cpp").read_text(), "int x = 0;\n")  # reverted

    def test_red_when_patch_does_not_apply(self):
        o, _ = self._verify({"patch": "not a real diff\n", "probe": "x",
                             "expect_error": False})
        self.assertEqual(o, "red")


class OpenPrTest(unittest.TestCase):
    def setUp(self):
        d = tmpdir()
        self.repo = d / "repo"
        self.repo.mkdir()
        (self.repo / "foo.cpp").write_text("int x = 0;\n")
        git(self.repo, "init", "-q")
        git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.repo, "config", "user.email", "d@d.invalid")
        git(self.repo, "config", "user.name", "d")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.conn = pack_db(d / "t.db")

    def test_staged_commits_branch_locally_without_write(self):
        os.environ.pop("FORGE_WRITE", None)
        with tx(self.conn):
            fid = _finding(self.conn, state="verifying", repo=str(self.repo))
            self.conn.execute("UPDATE items SET branch='forgeflow/fix-BUG-1'"
                              " WHERE id=?", (fid,))
        (self.repo / "foo.cpp").write_text("int x = 1; // fix\n")   # applied patch
        with tx(self.conn):
            o, r = get("forge.open_pr").fn(
                _ctx(self.conn, repo=str(self.repo), base="main",
                     pr_create_url="http://unused"), _task(), {})
        self.assertEqual(o, "staged")
        branches = subprocess.run(["git", "-C", str(self.repo), "branch"],
                                  capture_output=True, text=True).stdout
        self.assertIn("forgeflow/fix-BUG-1", branches)              # branch created + committed


class FixAbandonTest(unittest.TestCase):
    def test_abandon_moves_to_failed(self):
        conn = pack_db(tmpdir() / "t.db")
        with tx(conn):
            _finding(conn, state="fixing")
        with tx(conn):
            o, r = get("fix.abandon").fn(_ctx(conn), _task(), {"why": "gave up"})
        self.assertEqual(o, "failed")
        self.assertEqual([s for s in r["_staged"]
                          if s["op"] == "transition"][0]["to_state"], "failed")


if __name__ == "__main__":
    unittest.main()
