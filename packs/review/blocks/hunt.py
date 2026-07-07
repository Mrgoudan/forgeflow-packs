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
from forgeflow.contract import context_provider
from forgeflow.util import run_cmd, template


def _tpl(value, task, prev):
    return template(value, {"payload": task.get("payload") or {}, "prev": prev or {}})


def _norm(s):
    return "\n".join(line.rstrip() for line in (s or "").strip().splitlines())


@block("hunt.probe_sweep", "state", {"clean", "findings", "error", "timeout"},
       required_params={"probes_dir", "cmd"})
def hunt_probe_sweep(ctx, task, prev):
    """mode:
      oracle (default) — classify each probe vs its recorded .expected.stderr.
      record           — run against BASE clang, save each output; report clean.
      diff             — run against PR clang, FLAG probes whose output differs
                         from the recorded base (a true head-vs-base behavior
                         diff — no stale-oracle ambiguity).
    A per-probe timeout (a hang) is always a finding.
    """
    probes_dir = _tpl(ctx["probes_dir"], task, prev)
    cmd_tpl = ctx["cmd"]
    mode = ctx.get("mode", "oracle")
    workers = int(ctx.get("max_workers", 6))
    ptimeout = int(ctx.get("probe_timeout_s", 60))
    step_dir = Path(ctx["_step_dir"])
    tools = ctx.get("_tools")
    # base outputs shared between the record and diff steps of the same task
    baseline = Path(ctx.get("baseline_dir")
                    or (Path(ctx.get("_data_dir", str(step_dir))) / "tasks"
                        / str(task["id"]) / "probe_baseline"))
    baseline.mkdir(parents=True, exist_ok=True)
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
            return {"id": pid, "outcome": "timeout"}
        actual = _norm(Path(errp).read_text(errors="replace").replace(probe, "<probe>"))
        if mode == "record":
            (baseline / (pid + ".out")).write_text(actual)
            return {"id": pid, "outcome": "recorded"}
        if mode == "diff":
            bf = baseline / (pid + ".out")
            if not bf.is_file():
                return {"id": pid, "outcome": "no_baseline"}
            base = bf.read_text(errors="replace")
            return {"id": pid, "outcome": "flip" if actual != base else "same",
                    "base": base, "head": actual}
        exp = Path(probe[:-4] + ".expected.stderr")
        expected = exp.read_text(errors="replace") if exp.is_file() else ""
        return {"id": pid,
                "outcome": "pass" if actual == _norm(expected) else "mismatch"}

    # PARALLEL fan-out, capped; DETERMINISTIC collection (sort by probe id)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(run_one, probes))
    results.sort(key=lambda r: r["id"])

    branch = (task.get("payload") or {}).get("branch", "?")
    repo = _tpl(ctx.get("repo", ""), task, prev)
    finding_outcomes = {"flip", "mismatch", "timeout"}
    staged, fails = [], []
    for r in results:
        if r["outcome"] not in finding_outcomes:
            continue
        fails.append(r)
        if r["outcome"] == "flip":
            title = ("probe %s: behavior CHANGED base->PR (base %r -> PR %r) — "
                     "confirm the change is intended" % (
                         r["id"], (r.get("base") or "<no diagnostic>")[:80],
                         (r.get("head") or "<no diagnostic>")[:80]))
        elif r["outcome"] == "timeout":
            title = "probe %s: the compiler HUNG on this input" % r["id"]
        else:
            title = "probe %s: diverges from its recorded oracle" % r["id"]
        staged.append({
            "op": "upsert_finding", "key": "sweep-%s-%s" % (branch, r["id"]),
            "title": title, "source": "review", "repo": str(repo),
            "severity": "high" if r["outcome"] == "timeout" else "medium",
            "pattern": "probe-%s" % r["outcome"]})
    return ("findings" if fails else "clean"), dict(
        carry, mode=mode, total=len(results), failed=len(fails),
        results=[{"id": r["id"], "outcome": r["outcome"]} for r in results],
        _staged=staged)


@context_provider("probe_results")
def _probe_results(env, task, spec):
    """The probe sweep's outcomes for this branch, as review context: which
    probes DIVERGED from their oracle against the PR build. A diverged probe
    that exercises the changed code is strong evidence the diff altered
    behavior — the agent should check whether it's intended."""
    branch = (task.get("payload") or {}).get("branch", "")
    prefix = "sweep-%s-" % branch
    diverged = []
    for r in env.conn.execute(
            "SELECT key, pattern FROM findings WHERE source='review'"
            " AND key LIKE 'sweep-' || ? || '-%' ORDER BY key", (branch,)):
        diverged.append({"probe": r["key"][len(prefix):],
                         "outcome": (r["pattern"] or "").replace("probe-", "")})
    if not diverged:
        return {"note": "All probes matched their oracle against the build."}
    return {"note": "These probes DIVERGED from their recorded oracle against "
                    "the PR build — determine whether the diff caused it.",
            "diverged": diverged}


@block("evidence.build", "local", {"green", "red", "error", "timeout"},
       required_params={"cmd"})
def evidence_build(ctx, task, prev):
    """Build the code under review (the evidence gate's compile step). green
    = exit 0; red = the PR does not build (itself a high finding). Classify
    by exit code ONLY. Carries the worktree path forward. The build makes
    the pack's clang reflect the PR so the probe sweep tests PR behavior,
    not base drift."""
    import subprocess as _sp
    cmd = [template(c, {"payload": task.get("payload") or {}, "prev": prev or {}})
           for c in ctx["cmd"]]
    cwd = ctx.get("cwd")
    if cwd:
        cwd = template(cwd, {"payload": task.get("payload") or {}})
    carry = {"path": (prev or {}).get("path"),
             "diff_file": (prev or {}).get("diff_file")}
    try:
        code, out, err = run_cmd(cmd, ctx["_timeout_s"],
                                 Path(ctx["_step_dir"]) / "build",
                                 cwd=cwd, tools=ctx.get("_tools"))
    except _sp.TimeoutExpired:
        raise
    if code != 0:
        branch = (task.get("payload") or {}).get("branch", "?")
        return "red", dict(carry, exit_code=code, stderr_path=err, _staged=[{
            "op": "upsert_finding", "key": "build-%s" % branch,
            "title": "PR does not build (exit %d)" % code, "source": "review",
            "repo": str(template(ctx.get("repo", ""), {})), "severity": "high",
            "pattern": "red-build"}])
    return "green", dict(carry, exit_code=0)
