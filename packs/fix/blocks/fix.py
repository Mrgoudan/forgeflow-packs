"""The auto-fix loop — drive a triaged finding through the engine's finding
state machine to an open PR:

    triaged --prepare--> fixing --verify(green)--> verifying --open_pr--> pr_open
                              \--verify(red)/no-fix--> failed  (human requeues)

Every state change goes through the engine's transition() (enforced,
audited) via staged {op: transition} ops. The blocks are generic over the
finding's SOURCE (a hunt finding and a review finding fix the same way);
the build/probe commands are pack config, so nothing here names a compiler.

- fix.prepare   triaged -> fixing; name the fix branch; expose the evidence.
- fix.verify    apply the patch, build, re-run the finding's probe (must now
                behave CORRECTLY), fixing -> verifying; green/red.
- fix.abandon   -> failed (patch didn't apply / build broke / probe still
                wrong / the model gave up). A human can requeue from failed.
"""
from __future__ import annotations

import json
from pathlib import Path

from forgeflow.blocks import block
from forgeflow.contract import context_provider
from forgeflow.util import run_cmd, template


def _finding(conn, key):
    return conn.execute(
        "SELECT id, key, title, detail, state, severity, pattern, repo, branch"
        " FROM findings WHERE key=?", (key,)).fetchone()


@context_provider("fix_target")
def _fix_target(env, task, spec):
    """The defect to fix: its title, evidence, root-cause pattern, and the
    repro. The fixer reads this + the region code + the manual and returns a
    patch. Looked up by the finding key on the task payload."""
    key = (task.get("payload") or {}).get("finding")
    if not key:
        return {}
    r = _finding(env.conn, key)
    if not r:
        return {}
    try:
        evidence = json.loads(r["detail"] or "{}")
    except ValueError:
        evidence = {"detail": r["detail"]}
    return {"key": r["key"], "title": r["title"], "severity": r["severity"],
            "pattern": r["pattern"], "evidence": evidence,
            "note": "Produce a MINIMAL patch that makes the compiler handle "
                    "the repro correctly WITHOUT regressing other behavior. "
                    "Return the repro you fixed so the oracle can verify it."}


@block("fix.prepare", "state", {"prepared", "skip"}, required_params={"repo"})
def fix_prepare(ctx, task, prev):
    """triaged -> fixing. Name the fix branch (recorded on the finding) so the
    open_pr step knows where to commit. Skips a finding that isn't triaged
    (already fixing, rejected, merged, ...) — idempotent re-entry."""
    conn = ctx["_conn"]
    key = (task.get("payload") or {}).get("finding")
    r = _finding(conn, key) if key else None
    if not r or r["state"] != "triaged":
        return "skip", {"reason": "not triaged", "finding": key,
                        "state": r["state"] if r else None}
    prefix = template(ctx.get("branch_prefix", "forgeflow/fix-"), {})
    branch = (prefix + key)[:200]
    conn.execute("UPDATE findings SET branch=? WHERE id=?", (branch, r["id"]))
    return "prepared", {"finding": key, "branch": branch,
                        "_staged": [{"op": "transition", "finding_id": r["id"],
                                     "to_state": "fixing", "event": "fix:started",
                                     "evidence": {"branch": branch}}]}


@block("fix.verify", "local", {"green", "red", "error", "timeout"},
       required_params={"repo", "clang", "build_cmd"})
def fix_verify(ctx, task, prev):
    """Apply the proposed patch, build the compiler, and re-run the finding's
    repro against the patched build: the invariant must now HOLD (unsafe code
    rejected / safe code accepted / no crash). fixing -> verifying. A patch
    that doesn't apply, breaks the build, or leaves the repro still wrong is
    'red'. The working tree is reset on red so the next attempt starts clean.
    (Regression breadth — the full probe sweep — is the caller's next step.)"""
    conn = ctx["_conn"]
    repo = template(ctx["repo"], {})
    res = prev or {}
    key = (task.get("payload") or {}).get("finding")
    r = _finding(conn, key) if key else None
    if not r or r["state"] != "fixing":
        return "error", {"reason": "not fixing", "finding": key}
    patch = res.get("patch") or ""
    probe = res.get("probe") or ""
    expect_error = bool(res.get("expect_error"))
    sd = Path(ctx["_step_dir"])
    sd.mkdir(parents=True, exist_ok=True)          # engine doesn't pre-create it
    tools = ctx.get("_tools")

    staged = [{"op": "transition", "finding_id": r["id"], "to_state": "verifying",
               "event": "fix:built"}]

    def red(why, extra=None):
        run_cmd(["git", "-C", repo, "checkout", "--", "."], 60, sd / "reset", tools=tools)
        ev = dict(extra or {}, why=why)
        staged[0]["evidence"] = ev
        return "red", {"finding": key, "why": why, "_staged": staged}

    # 1. apply the patch
    pf = sd / "fix.patch"
    pf.write_text(patch)
    code, _o, _e = run_cmd(["git", "-C", repo, "apply", "--whitespace=nowarn",
                            str(pf)], ctx["_timeout_s"], sd / "apply", tools=tools)
    if code != 0:
        return red("patch did not apply")

    # 2. build the compiler with the patch in the tree
    bcmd = [template(x, {}) for x in ctx["build_cmd"]]
    bcode, _o, be = run_cmd(bcmd, ctx["_timeout_s"], sd / "build", tools=tools)
    if bcode is None:
        return red("build timed out")
    if bcode != 0:
        return red("build failed", {"stderr_path": be})

    # 3. re-run the repro: it must now behave correctly
    clang = template(ctx["clang"], {})
    pr = sd / "repro.cbs"
    pr.write_text(probe)
    cmd = [clang, "-fsyntax-only", "-Wno-nullability-completeness"]
    inc = ctx.get("include")
    if inc:
        cmd += ["-I", template(inc, {})]
    cmd.append(str(pr))
    ccode, _o, cerr = run_cmd(cmd, ctx["_timeout_s"], sd / "repro", tools=tools)
    if ccode is None:
        return red("repro compile timed out")
    crashed = ccode > 128
    errored = ccode != 0
    # fixed == the compiler now does the right thing on the repro
    correct = (not crashed) and ((errored and expect_error) or
                                 ((not errored) and not expect_error))
    ev = {"exit_code": ccode, "expect_error": expect_error,
          "stderr_path": cerr}
    if not correct:
        return red("repro still wrong after patch", ev)
    staged[0]["evidence"] = ev
    return "green", {"finding": key, "branch": r["branch"], "_staged": staged}


@block("fix.abandon", "state", {"failed"})
def fix_abandon(ctx, task, prev):
    """Move the finding to 'failed' (a human can requeue). Reached when the
    model won't propose a fix or verify came back red."""
    conn = ctx["_conn"]
    key = (task.get("payload") or {}).get("finding")
    r = _finding(conn, key) if key else None
    if not r or r["state"] not in ("fixing", "verifying"):
        return "failed", {"finding": key, "noop": True}
    return "failed", {"finding": key,
                      "_staged": [{"op": "transition", "finding_id": r["id"],
                                   "to_state": "failed", "event": "fix:abandoned",
                                   "evidence": (prev or {}).get("why")}]}
