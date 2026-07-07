"""Forge boundary blocks (layer 2): poll PRs, fetch a PR head, post a
comment. Everything is config-driven URLs + a token ref — no forge names
in code, so any gitee/gitcode/github-shaped REST API plugs in via
project.yaml.

Determinism rules honored:
- classification from HTTP status codes and git exit codes ONLY;
- replay safety by content: pr.updated events dedup on (pr, head_sha),
  comments dedup on (kind, target, body_sha);
- FORGE_WRITE=1 gates real sends; without it egress archives only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from forgeflow.blocks import block
from forgeflow.config import load_secrets
from forgeflow.util import ensure_tx, run_cmd, sha256_text, template


def _tpl(value, ctx, task, prev):
    return template(value, {"payload": task.get("payload") or {},
                            "prev": prev or {}})


def _forge_get(url, auth, timeout_s):
    """GET with configured auth. Returns (status, parsed_json | None)."""
    headers = {}
    if auth and auth.get("token_ref"):
        token = load_secrets().get("FORGE_TOKEN_%s" % auth["token_ref"])
        if not token:
            return 401, None
        if auth.get("style") == "query":
            sep = "&" if "?" in url else "?"
            url = "%s%s%s=%s" % (url, sep, auth.get("name", "access_token"), token)
        else:
            headers[auth.get("name", "PRIVATE-TOKEN")] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except OSError:
        return 599, None       # unreachable == server-class trouble


@block("forge.poll_prs", "local",
       {"ok", "forge_auth", "forge_server", "timeout"},
       required_params={"prs_url"})
def forge_poll_prs(ctx, task, prev):
    """List open PRs; stage one pr.updated event per (pr, head_sha). The
    queue's payload-hash dedup makes every poll replay-safe — no watermark
    needed for correctness, only (later) for API quota."""
    status, prs = _forge_get(_tpl(ctx["prs_url"], ctx, task, prev),
                             ctx.get("auth"), ctx["_timeout_s"])
    if status in (401, 403):
        return "forge_auth", {"status": status}
    if status != 200 or not isinstance(prs, list):
        return "forge_server", {"status": status}
    staged, seen = [], []
    for pr in prs:
        try:
            payload = {"pr": int(pr["number"]),
                       "source_branch": pr["head"]["ref"],
                       "head_sha": pr["head"]["sha"],
                       "target_branch": pr["base"]["ref"],
                       "title": str(pr.get("title", ""))[:300],
                       "description": str(pr.get("body", ""))[:2000]}
        except (KeyError, TypeError, ValueError):
            continue           # malformed entry: skip, never guess
        staged.append({"op": "emit_event", "name": "pr.updated",
                       "payload": payload})
        seen.append(payload["pr"])
    return "ok", {"open_prs": seen, "_staged": staged}


@block("forge.fetch_pr_head", "local", {"ok", "error", "timeout"},
       required_params={"repo"})
def forge_fetch_pr_head(ctx, task, prev):
    """Fetch the PR's source ref from the remote and pin a local branch
    pr-<n> at its head — git plumbing only, verified by exit codes."""
    repo = _tpl(ctx["repo"], ctx, task, prev)
    remote = ctx.get("remote", "origin")
    ref = _tpl(ctx.get("ref", "{payload.source_branch}"), ctx, task, prev)
    local = _tpl(ctx.get("local_branch", "pr-{payload.pr}"), ctx, task, prev)
    code, out, err = run_cmd(["git", "-C", repo, "fetch", remote, ref],
                             ctx["_timeout_s"], Path(ctx["_step_dir"]) / "fetch",
                             tools=ctx.get("_tools"))
    if code != 0:
        return "error", {"exit_code": code, "stderr_path": err}
    code, out, err = run_cmd(["git", "-C", repo, "branch", "-f", local,
                              "FETCH_HEAD"],
                             ctx["_timeout_s"], Path(ctx["_step_dir"]) / "branch",
                             tools=ctx.get("_tools"))
    if code != 0:
        return "error", {"exit_code": code, "stderr_path": err}
    return "ok", {"branch": local, "pr": (task.get("payload") or {}).get("pr")}


@block("publish.comment", "egress",
       {"sent", "archived", "duplicate", "skipped", "leak_blocked",
        "forge_auth", "forge_server", "timeout"},
       required_params={"comment_url"})
def publish_comment(ctx, task, prev):
    """Egress choke point, per the engine contract: leak scan -> egress row
    + archived body (transaction) -> forge call -> record forge-side id.
    Idempotent on (kind, target, body_sha): a replay returns 'duplicate'.
    Without FORGE_WRITE=1 the comment is archived, never sent."""
    payload = task.get("payload") or {}
    request = payload.get("request") or {}
    if not request.get("pr"):
        return "skipped", {"reason": "no pr in request (local review)"}
    findings = payload.get("findings") or []
    rank = {"low": 1, "medium": 2, "high": 3}
    floor = rank.get(ctx.get("min_severity", "low"), 0)
    posted = [f for f in findings if rank.get(f.get("severity"), 1) >= floor]
    # Plain, useful wording — no internal jargon.
    lines = ["🤖 Automated BSC review — PR #%s" % request["pr"], ""]
    if posted:
        lines.append("**Verdict: %d issue(s) found.**" % len(posted))
        lines.append("")
        for f in posted:
            lines.append("- **[%s]** %s" % (f.get("severity", "?"),
                                            f.get("title", "").strip()))
    else:
        lines.append("**Verdict: no defects found.**")
    body = "\n".join(lines)
    for pattern in ctx.get("deny_patterns", ()):
        if re.search(pattern, body):
            return "leak_blocked", {"pattern": pattern}
    url = _tpl(ctx["comment_url"], ctx, task, prev)
    target = "pr#%s" % request["pr"]
    body_sha = sha256_text(body)
    conn = ctx["_conn"]

    with ensure_tx(conn):
        row = conn.execute(
            "SELECT id, forge_id FROM egress WHERE kind='comment' AND"
            " target=? AND body_sha=?", (target, body_sha)).fetchone()
        if row and row["forge_id"]:
            return "duplicate", {"egress_id": row["id"],
                                 "forge_id": row["forge_id"]}
        if row:
            egress_id = row["id"]
        else:
            body_path = Path(ctx["_step_dir"]) / "body.md"
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(body)
            egress_id = conn.execute(
                "INSERT INTO egress(kind, target, body_sha, body_path, task_id)"
                " VALUES ('comment',?,?,?,?)",
                (target, body_sha, str(body_path), task["id"])).lastrowid

    if os.environ.get("FORGE_WRITE") != "1":
        return "archived", {"egress_id": egress_id, "body_sha": body_sha}

    headers = {"Content-Type": "application/json"}
    auth = ctx.get("auth")
    if auth and auth.get("token_ref"):
        token = load_secrets().get("FORGE_TOKEN_%s" % auth["token_ref"])
        if not token:
            return "forge_auth", {"detail": "FORGE_TOKEN_%s missing" % auth["token_ref"]}
        if auth.get("style") == "query":
            sep = "&" if "?" in url else "?"
            url = "%s%s%s=%s" % (url, sep, auth.get("name", "access_token"), token)
        else:
            headers[auth.get("name", "PRIVATE-TOKEN")] = token
    req = urllib.request.Request(
        url, data=json.dumps({"body": body}).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=ctx["_timeout_s"]) as resp:
            forge_resp = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "forge_auth", {"status": e.code, "egress_id": egress_id}
        return "forge_server", {"status": e.code, "egress_id": egress_id}
    except OSError as e:
        return "forge_server", {"detail": str(e), "egress_id": egress_id}
    forge_id = str(forge_resp.get("id", ""))
    with ensure_tx(conn):
        conn.execute("UPDATE egress SET forge_id=? WHERE id=?",
                     (forge_id, egress_id))
    return "sent", {"egress_id": egress_id, "forge_id": forge_id,
                    "target": target}
