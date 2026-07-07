from __future__ import annotations

import unittest

from helpers import PACKS, tmpdir

from forgeflow.blocks import load_files, run_isolated

# register the generic hunt block via the engine loader (idempotent)
load_files([str(PACKS / "packs" / "review" / "blocks" / "hunt.py")])


class ProbeSweepTest(unittest.TestCase):
    """hunt.probe_sweep: parallel, oracle-classified, deterministic. Uses a
    fake 'compiler' script (cat the probe to stderr) so no clang is needed."""

    def setUp(self):
        self.dir = tmpdir()
        self.probes = self.dir / "probes"
        self.probes.mkdir()
        # fake compiler: echoes the probe's body to stderr
        self.cc = self.dir / "cc.sh"
        self.cc.write_text("#!/bin/sh\ncat \"$1\" 1>&2\n")
        self.cc.chmod(0o755)

    def _probe(self, name, body, expected):
        (self.probes / (name + ".cbs")).write_text(body)
        (self.probes / (name + ".expected.stderr")).write_text(expected)

    def _sweep(self, **extra):
        ctx = {"probes_dir": str(self.probes),
               "cmd": [str(self.cc), "{probe}"], "max_workers": 4,
               "probe_timeout_s": 5}
        ctx.update(extra)
        return run_isolated("hunt.probe_sweep", ctx,
                            task={"id": 1, "attempts": 0,
                                  "payload": {"branch": "b"}},
                            prev={"path": "/ws", "diff_file": "review.diff"})

    def test_clean_when_all_match(self):
        # actual stderr == probe body; expected == body -> match. But the
        # oracle normalizes the probe PATH to <probe>; body has no path -> ok.
        self._probe("a", "hello\n", "hello")
        self._probe("b", "world\n", "world")
        outcome, res = self._sweep()
        self.assertEqual(outcome, "clean")
        self.assertEqual(res["failed"], 0)
        self.assertEqual(res["path"], "/ws")            # carries worktree fwd
        self.assertEqual(res["_staged"], [])            # nothing filed when clean

    def test_mismatch_is_a_finding(self):
        self._probe("good", "ok\n", "ok")
        self._probe("drift", "actual output\n", "DIFFERENT expected")
        outcome, res = self._sweep()
        self.assertEqual(outcome, "findings")
        self.assertEqual(res["failed"], 1)
        keys = [op["key"] for op in res["_staged"]]
        self.assertEqual(keys, ["sweep-b-drift"])
        self.assertEqual(res["_staged"][0]["severity"], "medium")

    def test_timeout_probe_is_high_finding(self):
        # a probe whose compile hangs -> per-probe timeout -> high finding
        self.cc.write_text("#!/bin/sh\ncase \"$1\" in *hang*) sleep 30;; "
                           "*) cat \"$1\" 1>&2;; esac\n")
        self.cc.chmod(0o755)
        self._probe("fine", "x\n", "x")
        self._probe("hang", "y\n", "y")
        outcome, res = self._sweep(probe_timeout_s=1)
        self.assertEqual(outcome, "findings")
        f = [op for op in res["_staged"] if op["key"] == "sweep-b-hang"][0]
        self.assertEqual(f["severity"], "high")
        self.assertIn("timeout", f["pattern"])

    def test_deterministic_order(self):
        for n in "cdaeb":
            self._probe(n, n + "\n", "MISMATCH")     # all fail
        _, res = self._sweep()
        ids = [r["id"] for r in res["results"]]
        self.assertEqual(ids, sorted(ids))            # sorted, not run-order
        keys = [op["key"] for op in res["_staged"]]
        self.assertEqual(keys, sorted(keys))

    def test_no_probes_is_error(self):
        outcome, res = self._sweep()
        self.assertEqual(outcome, "error")

    def test_head_vs_base_flip(self):
        # record base outputs, then diff after the "compiler" changes behavior
        self._probe("stable", "same\n", "ignored")
        self._probe("changes", "before\n", "ignored")
        baseline = str(self.dir / "bl")
        o, _ = self._sweep(mode="record", baseline_dir=baseline)
        self.assertEqual(o, "clean")
        # now the compiler emits different output ONLY for 'changes'
        self.cc.write_text('#!/bin/sh\ncase "$1" in *changes*) echo after 1>&2;; '
                           '*) cat "$1" 1>&2;; esac\n')
        self.cc.chmod(0o755)
        o, res = self._sweep(mode="diff", baseline_dir=baseline)
        self.assertEqual(o, "findings")
        keys = [op["key"] for op in res["_staged"]]
        self.assertEqual(keys, ["sweep-b-changes"])            # only the flip
        self.assertEqual(res["_staged"][0]["pattern"], "probe-flip")
        self.assertIn("base", res["_staged"][0]["title"])


if __name__ == "__main__":
    unittest.main()
