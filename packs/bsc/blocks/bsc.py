"""BSC domain context + the manual gate (tier 3).

The BiSheng C user manual lives INSIDE the reviewed repo
(`clang/docs/BSC/BiShengCLanguageUserManual.md`). It is ground truth: the
bsc-* skills are distilled from it and may lag, so the manual overrides the
skills. And because it is ground truth, a PR that changes BSC semantics must
update it — a semantics change with no manual edit is itself a finding.

Determinism: "the manual changed" is a git blob-hash comparison; "semantics
touched without the manual" is a diff name-list check. No guessing.
"""
from __future__ import annotations

from pathlib import Path

from forgeflow.blocks import block
from forgeflow.contract import context_provider
from forgeflow.util import run_cmd

_MAX_BYTES = 6000


def _git(env, repo, args, sub):
    out_dir = Path(env.data_dir) / "tasks" / "bsc" / sub
    code, out, err = run_cmd(["git", "-C", str(repo)] + args, 60, out_dir,
                             tools=(env.pack.tools or {}))
    return code, Path(out).read_text(errors="replace")


@context_provider("bsc_manual")
def _bsc_manual(env, task, spec):
    """The in-repo manual as authoritative ground truth. Read at the branch
    under review (so it reflects the manual AS UPDATED in this PR); flag if
    its blob differs from the rev the skills were validated against."""
    paths = env.pack.paths or {}
    params = env.pack.params or {}
    repo, manual_path = paths.get("repo"), params.get("manual_path")
    if not (repo and manual_path):
        return {}
    branch = (task.get("payload") or {}).get("branch") or "HEAD"
    ref = "%s:%s" % (branch, manual_path)
    code, blob = _git(env, repo, ["rev-parse", ref], "manual-blob")
    if code != 0:
        return {"status": "MISSING", "authoritative": True,
                "manual_path": manual_path,
                "note": "The user manual %s is absent at %s — it is ground "
                        "truth and must exist before review." % (manual_path, branch)}
    blob = blob.strip()
    pinned = params.get("manual_pinned_sha")
    code, content = _git(env, repo, ["show", ref], "manual-show")
    excerpt = content[:_MAX_BYTES] if code == 0 else ""
    changed = bool(pinned) and blob != pinned
    note = ("The authoritative BiSheng C user manual is at %s in THIS "
            "checkout. It is ground truth and OVERRIDES any bsc-* skill that "
            "disagrees — open the file for any rule you rely on. " % manual_path)
    if changed:
        note += ("It CHANGED since the skills were validated (blob %s vs "
                 "pinned %s); treat skills as suspect where they differ."
                 % (blob[:12], pinned[:12]))
    return {"status": "CHANGED" if changed else "current",
            "authoritative": True, "manual_path": manual_path,
            "manual_blob": blob, "pinned_blob": pinned,
            "note": note, "excerpt": excerpt}


@block("bsc.manual_gate", "state", {"ok", "flagged", "error", "timeout"},
       required_params={"repo", "manual_path"})
def bsc_manual_gate(ctx, task, prev):
    """No-AI gate: a PR whose diff touches a BSC semantics prefix but NOT the
    manual file gets a machine finding — the manual is ground truth and must
    be updated before review. The finding uses the machine-finding key
    convention so adjudicate triages and posts it."""
    payload = task.get("payload") or {}
    base, branch = payload.get("base"), payload.get("branch")
    repo, manual_path = ctx["repo"], ctx["manual_path"]
    prefixes = ctx.get("semantics_prefixes") or []
    carry = {"path": (prev or {}).get("path"),
             "diff_file": (prev or {}).get("diff_file")}
    if not (base and branch):
        return "ok", carry
    out_dir = Path(ctx["_step_dir"]) / "diff-names"
    code, out, err = run_cmd(
        ["git", "-C", str(repo), "diff", "--name-only", "%s...%s" % (base, branch)],
        ctx["_timeout_s"], out_dir, tools=ctx.get("_tools"))
    if code != 0:
        return "error", dict(carry, exit_code=code)
    touched = Path(out).read_text(errors="replace").split()
    semantics = [p for p in touched
                 if any(p.startswith(pre) for pre in prefixes)]
    if semantics and manual_path not in touched:
        finding = {
            "op": "upsert_finding",
            "key": "pattern-%s-manual-not-updated" % branch,
            "title": "BSC semantics changed without updating the user manual "
                     "(%s): %s" % (manual_path, ", ".join(semantics[:5])),
            "source": "review", "repo": str(repo), "severity": "medium",
            "pattern": "manual-not-updated",
        }
        return "flagged", dict(carry, _staged=[finding], semantics=semantics)
    return "ok", dict(carry, semantics=semantics)


@context_provider("bsc_notes")
def _bsc_notes(env, task, spec):
    """Point the agent at the compiler-internals notes so it reads what it
    needs DYNAMICALLY — no static path->note map. Returns the notes dir and
    its INDEX; the agent (agentic CLI) opens whichever notes are relevant to
    the files in the diff, the same way it invokes bsc-* skills on demand."""
    notes_dir = (env.pack.paths or {}).get("code_notes")
    if not notes_dir:
        return {}
    index = Path(notes_dir) / "INDEX.md"
    return {"dir": str(notes_dir),
            "index": index.read_text(errors="replace")[:8000] if index.is_file() else "",
            "note": "BSC compiler-internals notes live in `dir` (see `index`). "
                    "Open the ones relevant to the files you are reviewing."}
