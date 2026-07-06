"""Review-pack blocks (layer 2): the only custom Python this pack needs.

- review.diff     produce the diff under review inside the worktree
- review.file_findings  stage one findings row per agent claim

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
    """Turn the agent's verdict (a CLAIM) into findings rows in state
    'found'. The agent never touches the db; a later triage/evidence stage
    decides what the findings become."""
    payload = task.get("payload") or {}
    findings = (prev or {}).get("findings") or []
    staged = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict) or not f.get("title"):
            continue
        key = "review-%s-%d" % (payload.get("branch", "unknown"), i)
        staged.append({
            "op": "upsert_finding", "key": key, "title": f["title"][:200],
            "source": "review", "repo": str(ctx["repo"]),
            "detail": json.dumps(f, sort_keys=True),
            "severity": f.get("severity"),
        })
    return "ok", {"_staged": staged, "filed": len(staged),
                  "run_id": (prev or {}).get("_run_id")}
