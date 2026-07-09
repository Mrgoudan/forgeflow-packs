"""Review context providers + the no-AI pattern pre-scan (DATAMODEL §4).

Providers are how tier 2 reads the db for prompt assembly: declared per
step, assembled by the engine, pinned by prompt_sha. Blocks never query
the db ad hoc — a provider's output is the ONLY window a step gets.

- lessons:  distilled instructions for this task kind (learn workflow's
            output; empty table = empty list, never an error)
- history:  defect history of the files this branch touches
            (implications ⋈ items by path — "where the bodies are")
- patterns: active machine-checkable defect rules (patterns.grep_rule)

review.pattern_scan then runs those rules over the ADDED lines of the
diff — items from it are machine facts that post with every model
down. BERT/agents shape attention; these file evidence.
"""
from __future__ import annotations

import re
from pathlib import Path

from forgeflow.blocks import block
from forgeflow.contract import context_provider
from forgeflow.util import run_cmd


@context_provider("lessons")
def _lessons(env, task, spec):
    kind = spec.get("task_kind") or task.get("kind") or "review"
    return [r["rule"] for r in env.conn.execute(
        "SELECT rule FROM lessons WHERE task_kind=? ORDER BY id", (kind,))]


@context_provider("history")
def _history(env, task, spec):
    """{touched_path: [prior items implicated there]}. Touched paths
    come from git (deterministic), not from the model."""
    payload = task.get("payload") or {}
    repo = env.pack.paths.get("repo")
    base, branch = payload.get("base"), payload.get("branch")
    if not (repo and base and branch):
        return {}
    out_dir = Path(env.data_dir) / "tasks" / str(task["id"]) / "ctx-history"
    code, out, err = run_cmd(
        ["git", "-C", repo, "diff", "--name-only", "%s...%s" % (base, branch)],
        60, out_dir, tools=env.pack.tools)
    if code != 0:
        return {}
    history = {}
    for path in Path(out).read_text().split():
        rows = env.conn.execute(
            "SELECT f.key, f.title, f.state, i.role"
            " FROM code_objects co"
            " JOIN implications i ON i.object_id = co.id"
            " JOIN items f ON f.id = i.item_id"
            " WHERE co.path=? ORDER BY f.id", (path,)).fetchall()
        if rows:
            history[path] = [{"key": r["key"], "title": r["title"],
                              "state": r["state"], "role": r["role"]}
                             for r in rows]
    return history


@context_provider("patterns")
def _patterns(env, task, spec):
    """Active defect patterns. review_lens is the natural-language rung —
    the strong model is a far better fuzzy matcher than embedding cosine,
    so we TEACH it the pattern rather than trying to retrieve a match."""
    return [{"id": r["id"], "lens": r["review_lens"], "grep_rule": r["grep_rule"]}
            for r in env.conn.execute(
                "SELECT id, grep_rule, review_lens FROM patterns"
                " WHERE status='active' ORDER BY id")]


@context_provider("candidates")
def _candidates(env, task, spec):
    """The agent lens's own proposed items (state 'found'), for the
    refutation pass to vet. Machine (pattern-*) items are excluded —
    they are evidence, not claims, and are never refuted."""
    branch = (task.get("payload") or {}).get("branch", "")
    return [{"key": r["key"], "title": r["title"], "severity": r["severity"],
             "detail": r["detail"]}
            for r in env.conn.execute(
                "SELECT key, title, severity, detail FROM items"
                " WHERE source='review' AND state='found'"
                " AND key LIKE 'review-' || ? || '-%' ORDER BY id", (branch,))]


@block("review.pattern_scan", "state", {"clean", "hits", "timeout"},
       accepts_context={"patterns", "payload"})
def review_pattern_scan(ctx, task, prev):
    """Run every active pattern rule over the ADDED lines of review.diff.
    Each hit is a machine item (staged, state 'found') — evidence from
    a rule distilled out of every bug previously fixed. No model, no
    prose: a regex either matches an added line or it doesn't."""
    payload = task.get("payload") or {}
    ws = (prev or {}).get("path")
    if not ws:
        raise RuntimeError("review.pattern_scan: no worktree path in prev")
    diff_file = Path(ws) / (prev or {}).get("diff_file", "review.diff")
    added = [(n, line[1:]) for n, line in
             enumerate(diff_file.read_text(errors="replace").splitlines(), 1)
             if line.startswith("+") and not line.startswith("+++")]
    hits, staged = [], []
    for rule in ctx.get("patterns") or []:
        if not rule.get("grep_rule"):
            continue          # a lens-only pattern has no machine rule
        try:
            rx = re.compile(rule["grep_rule"])
        except re.error:
            continue          # a broken stored rule must not kill review
        for n, text in added:
            if rx.search(text):
                hits.append({"rule": rule["id"], "diff_line": n,
                             "text": text.strip()[:200]})
                staged.append({
                    "op": "upsert_item",
                    "key": "pattern-%s-%s-%d" % (payload.get("branch", "?"),
                                                 rule["id"], n),
                    "title": "pattern %s matched added line: %s"
                             % (rule["id"], text.strip()[:120]),
                    "source": "review", "repo": str(ctx.get("repo") or ""),
                    "pattern": rule["id"], "severity": "medium",
                })
    result = {"path": ws, "diff_file": (prev or {}).get("diff_file"),
              "hits": hits, "_staged": staged}
    return ("hits" if hits else "clean"), result
