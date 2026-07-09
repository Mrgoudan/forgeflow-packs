"""Review-pack blocks (layer 2): the only custom Python this pack needs.

- review.diff     produce the diff under review inside the worktree
- review.file_findings  stage one items row per agent claim

Both follow the block rules: outcomes from exit codes / structure only,
subprocesses via util.run_cmd, db writes staged (never committed here).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from forgeflow.blocks import block
from forgeflow.util import run_cmd, template


def _tpl(value, task, prev):
    return template(value, {"payload": task.get("payload") or {},
                            "prev": prev or {}})


@block("review.diff", "local", {"ok", "empty", "error", "timeout"})
def review_diff(ctx, task, prev):
    """git diff <base>...HEAD inside the worktree, written to ./review.diff
    so an agentic reviewer can read it in cwd. Carries the worktree path
    forward for the next step."""
    ws = (prev or {}).get("path")
    if not ws:
        raise RuntimeError("review.diff: no worktree path in prev result")
    base = _tpl(ctx.get("base") or "HEAD~1", task, prev)
    code, out, err = run_cmd(["git", "diff", "%s...HEAD" % base],
                             ctx["_timeout_s"], Path(ctx["_step_dir"]),
                             cwd=ws, tools=ctx.get("_tools"))
    if code != 0:
        return "error", {"exit_code": code, "stderr_path": err}
    text = Path(out).read_text(errors="replace")
    if not text.strip():
        return "empty", {"path": ws, "base": base}
    diff_file = Path(ws) / "review.diff"
    shutil.copyfile(out, str(diff_file))
    return "ok", {"path": ws, "diff_file": "review.diff", "base": base,
                  "diff_lines": len(text.splitlines())}


@block("review.file_findings", "state", {"ok"}, required_params={"repo"})
def review_file_findings(ctx, task, prev):
    """Turn the agent's verdict (a CLAIM) into items rows in state
    'found'. The agent never touches the db; a later triage/evidence stage
    decides what the items become."""
    payload = task.get("payload") or {}
    items = (prev or {}).get("items") or []
    staged, summary = [], []
    for i, f in enumerate(items):
        if not isinstance(f, dict) or not f.get("title"):
            continue
        key = "review-%s-%d" % (payload.get("branch", "unknown"), i)
        staged.append({
            "op": "upsert_item", "key": key, "title": f["title"][:200],
            "source": "review", "repo": str(ctx["repo"]),
            "detail": json.dumps(f, sort_keys=True),
            "severity": f.get("severity"),
        })
        summary.append({"title": f["title"][:200],
                        "severity": f.get("severity"),
                        "path": f.get("path")})
    return "ok", {"_staged": staged, "filed": len(staged),
                  "items": summary, "path": (prev or {}).get("path"),
                  "run_id": (prev or {}).get("_run_id")}


_SEV_RANK = {"low": 1, "medium": 2, "high": 3}


@block("review.adjudicate", "state", {"ok"})
def review_adjudicate(ctx, task, prev):
    """The verdict of the refutation pass, applied through the item
    state machine. Agent candidates the refuter CONFIRMED move found ->
    triaged (with the reason as evidence); REJECTED move found -> rejected.
    Machine (pattern-*) items are confirmed by construction -> triaged.
    Only triaged items reach egress; rejected ones are archived, not
    posted. The confirmed set (with severity) rides to review.completed."""
    conn = ctx["_conn"]
    payload = task.get("payload") or {}
    branch = payload.get("branch", "")
    run_id = (prev or {}).get("_run_id")
    decisions = {d["key"]: d for d in (prev or {}).get("decisions") or []}

    staged, confirmed = [], []

    def _lookup(key):
        r = conn.execute("SELECT id, title, severity, state FROM items"
                         " WHERE key=?", (key,)).fetchone()
        return r

    # agent candidates: adjudicate per the refuter's decision
    for r in conn.execute(
            "SELECT id, key, title, severity, state FROM items"
            " WHERE source='review' AND key LIKE 'review-' || ? || '-%'"
            " ORDER BY id", (branch,)):
        if r["state"] != "found":
            continue
        d = decisions.get(r["key"])
        if d and str(d.get("decision", "")).upper() == "CONFIRM":
            staged.append({"op": "transition", "item_id": r["id"],
                           "to_state": "triaged", "event": "review:confirmed",
                           "evidence": {"reason": d.get("reason", ""),
                                        "run_id": run_id}})
            confirmed.append({"key": r["key"], "title": r["title"],
                              "severity": r["severity"], "confidence": "vetted"})
        else:
            staged.append({"op": "transition", "item_id": r["id"],
                           "to_state": "rejected", "event": "review:refuted",
                           "evidence": {"reason": d.get("reason", "") if d else
                                        "not defended by refutation pass",
                                        "run_id": run_id}})

    # machine items that are REAL problems -> triaged (the manual gate, a
    # red build, a compiler crash/hang). A probe behavior CHANGE (probe-flip)
    # is EVIDENCE for the AI, not a defect — it stays 'found' and is never
    # posted.
    for r in conn.execute(
            "SELECT id, key, title, severity, state, pattern FROM items"
            " WHERE source='review' AND state='found'"
            " AND (key LIKE 'pattern-' || ? || '-%' OR key LIKE 'sweep-' || ? || '-%'"
            "      OR key LIKE 'build-' || ? || '%') ORDER BY id",
            (branch, branch, branch)):
        if r["pattern"] == "probe-flip":
            continue                       # behavior change = evidence, not a defect
        staged.append({"op": "transition", "item_id": r["id"],
                       "to_state": "triaged", "event": "review:machine_rule",
                       "evidence": {"kind": r["pattern"]}})
        confirmed.append({"key": r["key"], "title": r["title"],
                          "severity": r["severity"], "confidence": "machine"})

    confirmed.sort(key=lambda f: -_SEV_RANK.get(f.get("severity"), 0))
    return "ok", {"_staged": staged, "items": confirmed,
                  "confirmed": len(confirmed)}
