"""Parallel probe sweep — the deterministic no-AI hunt core.

Runs a bench of probes against a command (a compiler) CONCURRENTLY and
classifies each by comparing its stderr to a recorded `<probe>.expected.stderr`
oracle. A mismatch (behavior drifted) or a per-probe timeout (a hang) is a
finding.

Parallelism model (the answer to "how do we parallelize here"): the engine
loop stays sequential — this block is ONE task/step, but it fans out its
probes over a thread pool (cap = max_workers) through the util.run_cmd
subprocess choke point. Results are collected SORTED by probe id, so the
verdict is independent of completion order — parallelism never changes the
outcome.
"""
from __future__ import annotations

import concurrent.futures as cf
import glob
import subprocess
from pathlib import Path

from forgeflow.blocks import block
from forgeflow.util import run_cmd, template


def _tpl(value, task, prev):
    return template(value, {"payload": task.get("payload") or {}, "prev": prev or {}})


def _norm(s):
    return "\n".join(line.rstrip() for line in (s or "").strip().splitlines())


@block("hunt.probe_sweep", "state", {"clean", "findings", "error", "timeout"},
       required_params={"probes_dir", "cmd"})
def hunt_probe_sweep(ctx, task, prev):
    probes_dir = _tpl(ctx["probes_dir"], task, prev)
    cmd_tpl = ctx["cmd"]
    workers = int(ctx.get("max_workers", 6))
    ptimeout = int(ctx.get("probe_timeout_s", 60))
    step_dir = Path(ctx["_step_dir"])
    tools = ctx.get("_tools")
    carry = {"path": (prev or {}).get("path"),
             "diff_file": (prev or {}).get("diff_file")}

    probes = sorted(glob.glob(str(Path(probes_dir) / "*.cbs")))
    if not probes:
        return "error", dict(carry, reason="no probes in %s" % probes_dir)

    def run_one(probe):
        pid = Path(probe).stem
        cmd = [c.replace("{probe}", probe) for c in cmd_tpl]
        try:
            code, _out, errp = run_cmd(cmd, ptimeout, step_dir / pid, tools=tools)
        except subprocess.TimeoutExpired:
            return {"id": pid, "outcome": "timeout", "exit": None}
        actual = Path(errp).read_text(errors="replace").replace(probe, "<probe>")
        exp = Path(probe[:-4] + ".expected.stderr")
        expected = exp.read_text(errors="replace") if exp.is_file() else ""
        ok = _norm(actual) == _norm(expected)
        return {"id": pid, "outcome": "pass" if ok else "mismatch",
                "exit": code, "stderr_path": errp}

    # PARALLEL fan-out, capped; DETERMINISTIC collection (sort by probe id)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(run_one, probes))
    results.sort(key=lambda r: r["id"])

    branch = (task.get("payload") or {}).get("branch", "?")
    repo = _tpl(ctx.get("repo", ""), task, prev)
    staged, fails = [], []
    for r in results:
        if r["outcome"] == "pass":
            continue
        fails.append(r)
        staged.append({
            "op": "upsert_finding",
            "key": "sweep-%s-%s" % (branch, r["id"]),
            "title": "probe %s %s: BSC analyzer behavior differs from the "
                     "recorded oracle" % (r["id"], r["outcome"]),
            "source": "review", "repo": str(repo),
            "severity": "high" if r["outcome"] == "timeout" else "medium",
            "pattern": "probe-%s" % r["outcome"],
        })
    return ("findings" if fails else "clean"), dict(
        carry, total=len(results), failed=len(fails),
        results=[{"id": r["id"], "outcome": r["outcome"]} for r in results],
        _staged=staged)
