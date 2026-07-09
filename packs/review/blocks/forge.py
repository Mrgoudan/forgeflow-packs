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


@block("forge.open_pr", "egress",
       {"opened", "staged", "nothing", "forge_auth", "forge_server",
        "error", "timeout"},
       required_params={"repo", "pr_create_url", "base"})
def forge_open_pr(ctx, task, prev):
    """Commit the verified fix (already applied in the working tree by
    fix.verify) onto the finding's fix branch and open a PR. Egress choke
    point: without FORGE_WRITE=1 it commits the branch LOCALLY and returns
    'staged' (never pushes/opens). On a real open it pushes the branch, POSTs
    the PR, records pr_number, and moves the finding verifying -> pr_open."""
    conn = ctx["_conn"]
    repo = template(ctx["repo"], {})
    key = (task.get("payload") or {}).get("finding")
    r = conn.execute("SELECT id, title, state, branch FROM findings WHERE key=?",
                     (key,)).fetchone()
    if not r or r["state"] != "verifying" or not r["branch"]:
        return "error", {"reason": "finding not in verifying/branch unset",
                         "finding": key}
    branch, base = r["branch"], template(ctx["base"], {})
    sd = Path(ctx["_step_dir"])
    tools = ctx.get("_tools")

    def git(*args, name="git"):
        return run_cmd(["git", "-C", repo, *args], ctx["_timeout_s"],
                       sd / name, tools=tools)

    git("checkout", "-B", branch, name="branch")
    git("add", "-A", name="add")
    title = "fix: %s" % (r["title"] or key)
    body = "Automated fix for finding `%s`.\n\nProduced by forgeflow." % key
    code, _o, _e = git("commit", "-m", title, "-m", body, name="commit")
    if code != 0:                                   # nothing staged to commit
        return "nothing", {"finding": key, "reason": "empty commit"}

    if os.environ.get("FORGE_WRITE") != "1":
        return "staged", {"finding": key, "branch": branch,
                          "note": "committed locally; set FORGE_WRITE=1 to push+PR"}

    pcode, _o, pe = git("push", "-u", "origin", branch, "--force-with-lease",
                        name="push")
    if pcode != 0:
        return "forge_server", {"detail": "push failed", "stderr_path": pe}

    url = _tpl(ctx["pr_create_url"], ctx, task, prev)
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
    payload = {"title": title, "head": branch, "base": base, "body": body}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=ctx["_timeout_s"]) as resp:
            pr = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "forge_auth", {"status": e.code}
        return "forge_server", {"status": e.code}
    except OSError as e:
        return "forge_server", {"detail": str(e)}
    number = pr.get("number") or pr.get("iid") or pr.get("id")
    conn.execute("UPDATE findings SET pr_number=? WHERE id=?", (number, r["id"]))
    return "opened", {"finding": key, "branch": branch, "pr_number": number,
                      "_staged": [{"op": "transition", "finding_id": r["id"],
                                   "to_state": "pr_open", "event": "fix:pr_opened",
                                   "evidence": {"pr_number": number, "branch": branch}}]}


def _forge_post(url, data, auth, timeout_s):
    """POST json with configured auth. Returns (status, parsed_json | None)."""
    headers = {"Content-Type": "application/json"}
    if auth and auth.get("token_ref"):
        token = load_secrets().get("FORGE_TOKEN_%s" % auth["token_ref"])
        if not token:
            return 401, None
        if auth.get("style") == "query":
            sep = "&" if "?" in url else "?"
            url = "%s%s%s=%s" % (url, sep, auth.get("name", "access_token"), token)
        else:
            headers[auth.get("name", "PRIVATE-TOKEN")] = token
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except OSError:
        return 599, None


def _behavior(ev):
    """Derive (expected, actual) prose from the oracle's evidence — what the
    compiler SHOULD have done vs what it did. The oracle classifies by whether
    the probe should error (expect_error) and how the compiler actually exited;
    `why` names the divergence, `actual` is the real diagnostic it emitted."""
    why = (ev.get("why") or "").lower()
    code = ev.get("exit_code")
    diag = (ev.get("actual") or "").strip()
    if not diag and ev.get("stderr_path"):          # fall back to the run dir
        try:
            diag = Path(ev["stderr_path"]).read_text(errors="replace").strip()
        except OSError:
            diag = ""
    # scrub the machine's temp probe path out of the diagnostic (no local
    # filesystem paths in a public issue) -> "repro.cbs:line:col: error: ...".
    diag = re.sub(r"\S*cand\.cbs", "repro.cbs", diag)
    if "crash" in why:
        return ("The compiler should accept the repro or reject it with a clean "
                "diagnostic — never crash.",
                "The compiler **crashes** (exit %s)." % code, diag)
    if "missed diagnostic" in why or "accepted" in why:
        return ("The repro is **unsafe** BSC — the compiler must reject it with "
                "an ownership/safety diagnostic.",
                "The compiler **accepts it silently** (exit %s); the required "
                "diagnostic is missing." % code, "")
    if "false positive" in why or "rejected" in why:
        return ("The repro is **valid** BSC — the compiler should accept it and "
                "compile without error.",
                "The compiler **rejects valid code** (exit %s):" % code, diag)
    return ("(see repro)",
            (ev.get("why") or "diverges from base clang")
            + (" (exit %s)" % code if code is not None else ""), diag)


def _finding_report_body(title, key, severity, pattern, ev):
    """The comment body for one confirmed finding: root cause (the headline +
    class), the Expected/Actual behavior contrast (with the real diagnostic),
    and the full .cbs repro."""
    probe = (ev.get("probe") or "").strip()
    expected, actual, diag = _behavior(ev)
    lines = [
        "### %s" % (title or key), "",
        "**Severity:** %s   ·   **Root-cause class:** `%s`   ·   **Key:** `%s`"
        % (severity or "?", pattern or "—", key), "",
        "**Expected behavior:** %s" % expected, "",
        "**Actual behavior:** %s" % actual]
    if diag:
        lines += ["", "```", diag, "```"]
    lines += ["", "**Minimal repro (`.cbs`):**", "```c", probe, "```", "",
              "_Verified against base clang by forgeflow bug-hunt._"]
    return "\n".join(lines)


@block("forge.report_finding", "egress",
       {"commented", "archived", "duplicate", "skipped", "leak_blocked",
        "forge_auth", "forge_server", "timeout"},
       required_params={"issue_url", "issue_comment_url"})
def forge_report_finding(ctx, task, prev):
    """Report a CONFIRMED bug as a COMMENT under ONE umbrella issue (one issue
    per campaign, one comment per finding). The umbrella issue is created on
    first use and its number kept in watermark 'bughunt.issue'. Egress choke
    point: leak-scan -> egress row (dedup on the finding key) -> forge POST
    (only with FORGE_WRITE=1, else archived)."""
    conn = ctx["_conn"]
    payload = task.get("payload") or {}
    key = payload.get("finding_key") or payload.get("finding")
    if not key:
        return "skipped", {"reason": "no finding in payload"}
    r = conn.execute("SELECT id, key, title, detail, severity, pattern"
                     " FROM findings WHERE key=?", (key,)).fetchone()
    if not r:
        return "skipped", {"reason": "no such finding", "finding": key}
    try:
        ev = json.loads(r["detail"] or "{}")
    except ValueError:
        ev = {}
    # only report ORACLE-CONFIRMED findings: a real repro is the evidence. A
    # finding with no probe is a vault/catalogue entry that was never verified
    # against base clang this campaign — never file it (that produced the
    # "probe not recorded / observed: —" junk comments).
    if not (ev.get("probe") or "").strip():
        return "skipped", {"reason": "no probe (not oracle-confirmed)", "finding": key}
    body = _finding_report_body(r["title"], key, r["severity"], r["pattern"], ev)
    for pattern in ctx.get("deny_patterns", ()):
        if re.search(pattern, body):
            return "leak_blocked", {"pattern": pattern}
    target = "comment:%s" % key
    body_sha = sha256_text(body)
    with ensure_tx(conn):
        row = conn.execute("SELECT id, forge_id FROM egress WHERE kind='issue_comment'"
                           " AND target=?", (target,)).fetchone()
        if row and row["forge_id"]:
            return "duplicate", {"egress_id": row["id"], "forge_id": row["forge_id"]}
        if row:
            egress_id = row["id"]
        else:
            body_path = Path(ctx["_step_dir"]) / "comment.md"
            body_path.write_text(body)
            egress_id = conn.execute(
                "INSERT INTO egress(kind, target, body_sha, body_path, task_id)"
                " VALUES ('issue_comment',?,?,?,?)",
                (target, body_sha, str(body_path), task["id"])).lastrowid
    if os.environ.get("FORGE_WRITE") != "1":
        return "archived", {"egress_id": egress_id, "finding": key}

    auth = ctx.get("auth")
    # umbrella issue: create once, remember its number
    wm = conn.execute("SELECT cursor FROM watermarks WHERE scope='bughunt.issue'").fetchone()
    umbrella = wm["cursor"] if wm else None
    if not umbrella:
        idata = {"title": template(ctx.get("issue_title",
                 "[BSC] forgeflow bug-hunt — confirmed findings"), {}),
                 "body": "Umbrella issue for forgeflow bug-hunt. Every confirmed "
                         "bug (verified against base clang) is posted as a comment below."}
        if ctx.get("issue_repo"):
            idata["repo"] = template(ctx["issue_repo"], {})
        st, iss = _forge_post(_tpl(ctx["issue_url"], ctx, task, prev), idata,
                              auth, ctx["_timeout_s"])
        if st in (401, 403):
            return "forge_auth", {"status": st}
        if st >= 400 or not iss:
            return "forge_server", {"status": st}
        umbrella = str(iss.get("number") or iss.get("iid") or iss.get("id"))
        with ensure_tx(conn):
            conn.execute("INSERT OR REPLACE INTO watermarks(scope, cursor)"
                         " VALUES ('bughunt.issue', ?)", (umbrella,))
    # post the finding as a comment on the umbrella issue
    curl = "%s/%s/comments" % (template(ctx["issue_comment_url"], {}).rstrip("/"),
                               umbrella)
    st, cm = _forge_post(curl, {"body": body}, auth, ctx["_timeout_s"])
    if st in (401, 403):
        return "forge_auth", {"status": st, "issue": umbrella}
    if st >= 400:
        return "forge_server", {"status": st, "issue": umbrella}
    cid = str((cm or {}).get("id") or "")
    with ensure_tx(conn):
        conn.execute("UPDATE egress SET forge_id=? WHERE id=?",
                     (cid or umbrella, egress_id))
        ev["issue_number"] = umbrella
        conn.execute("UPDATE findings SET detail=? WHERE id=?",
                     (json.dumps(ev), r["id"]))
    return "commented", {"finding": key, "issue": umbrella, "comment_id": cid}
