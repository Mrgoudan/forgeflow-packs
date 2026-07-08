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

import re
from pathlib import Path

from forgeflow.blocks import block
from forgeflow.contract import context_provider
from forgeflow.util import run_cmd, template

_MAX_BYTES = 6000


def _t(value, task, prev):
    return template(value, {"payload": task.get("payload") or {}, "prev": prev or {}})
_HEADING = re.compile(r"^(#{1,4})\s+(.*)")


def _git(env, repo, args, sub):
    out_dir = Path(env.data_dir) / "tasks" / "bsc" / sub
    code, out, err = run_cmd(["git", "-C", str(repo)] + args, 60, out_dir,
                             tools=(env.pack.tools or {}))
    return code, Path(out).read_text(errors="replace")


_GUIDE_MAX = 14000     # the compiler dev guide is ~140 lines; inject it whole


@context_provider("bsc_compiler_guide")
def _bsc_compiler_guide(env, task, spec):
    """The BSC *compiler*-engineering guide: how to edit each subsystem (AST
    walkers, the ownership status-map encoding, borrow-checker dispatch, the
    safe-zone width matrix) and the recurring change-shapes. Distinct from the
    manual (the LANGUAGE spec) and the bsc-* skills (the language) — this is
    how to MODIFY the compiler, which the fixer and explorer need to write a
    correct patch / know where a defect lives. Read live from the git-tracked
    vault doc, so edits flow in without a re-port. It does NOT override the
    manual."""
    path = (env.pack.params or {}).get("compiler_guide")
    if not path:
        return {}
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return {}
    return {"guide": text[:_GUIDE_MAX],
            "note": "How this compiler is built and changed — BSC code is "
                    "ENABLE_BSC-guarded and isolated in BSC/ subdirs; the "
                    "recurring bug shapes are a dispatch table missing an AST "
                    "kind, a predicate defeated by a transparent wrapper, an "
                    "un-normalized encoded field. Use it to place/shape a "
                    "MINIMAL patch; the manual remains the language authority."}


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
    # inject the TABLE OF CONTENTS (markdown headings), not a blind first-N
    # slice (which was just the cover page). Lets the agent jump to the right
    # section and open the file there.
    toc = []
    if code == 0:
        for line in content.splitlines():
            m = _HEADING.match(line)
            if m:
                toc.append("%s %s" % ("  " * (len(m.group(1)) - 1), m.group(2).strip()))
    toc_text = "\n".join(toc)[:_MAX_BYTES]
    changed = bool(pinned) and blob != pinned
    note = ("The authoritative BiSheng C user manual is at %s in THIS "
            "checkout. It is ground truth and OVERRIDES any bsc-* skill that "
            "disagrees. Its table of contents is below; open the file at the "
            "section relevant to the code you review. " % manual_path)
    if changed:
        note += ("It CHANGED since the skills were validated (blob %s vs "
                 "pinned %s); treat skills as suspect where they differ."
                 % (blob[:12], pinned[:12]))
    # Soft signal (NOT an auto-finding): does the diff change BSC semantics
    # without touching the manual? Most compiler fixes legitimately don't
    # need a manual edit; the AI decides whether THIS change alters
    # documented language behavior. That judgment can't be a blind gate.
    base = (task.get("payload") or {}).get("base")
    sem_no_manual = []
    prefixes = params.get("semantics_prefixes") or []
    if base and branch and prefixes:
        code, names = _git(env, repo, ["diff", "--name-only",
                                       "%s...%s" % (base, branch)], "manual-diff")
        if code == 0:
            touched = names.split()
            if manual_path not in touched:
                sem_no_manual = [p for p in touched
                                 if any(p.startswith(pre) for pre in prefixes)][:8]
    result = {"status": "CHANGED" if changed else "current",
              "authoritative": True, "manual_path": manual_path,
              "manual_blob": blob, "pinned_blob": pinned,
              "note": note, "toc": toc_text}
    if sem_no_manual:
        result["semantics_changed_without_manual"] = sem_no_manual
        result["manual_note"] = (
            "This PR changes BSC semantics in %s but does NOT update the "
            "manual. Only raise this if the change alters DOCUMENTED language "
            "behavior (new/changed syntax, rules, or user-facing semantics). "
            "Internal diagnostic/analysis fixes normally need no manual update "
            "— do not flag those." % ", ".join(sem_no_manual))
    return result


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


@block("bsc.ensure_base", "local", {"cached", "built", "error", "timeout"},
       required_params={"repo", "build_dir", "baseline_root"})
def bsc_ensure_base(ctx, task, prev):
    """Ensure the base baseline is current, before each review. Pulls the
    base branch (git pull --ff-only — a no-op when nothing changed) and
    checks the baseline cache keyed by base rev:
      cached — base unchanged, baseline already recorded: FREE, skip rebuild.
      built  — base advanced (or first run): base clang rebuilt; the next
               step records its baseline.
    This is why refreshing every review is cheap: unchanged base = cache hit.
    """
    repo = _t(ctx["repo"], task, prev)
    build_dir = _t(ctx["build_dir"], task, prev)
    baseline_root = _t(ctx["baseline_root"], task, prev)
    base = (task.get("payload") or {}).get("base", "HEAD")
    sd = Path(ctx["_step_dir"])
    tools = ctx.get("_tools")
    carry = {"path": (prev or {}).get("path"),
             "diff_file": (prev or {}).get("diff_file")}

    code, _o, err = run_cmd(["git", "-C", repo, "checkout", base], 120,
                            sd / "checkout", tools=tools)
    if code != 0:
        return "error", dict(carry, reason="checkout %s failed" % base,
                             stderr_path=err)
    # pull latest base; tolerate failure (offline / no upstream)
    run_cmd(["git", "-C", repo, "pull", "--ff-only"], 300, sd / "pull", tools=tools)
    code, out, _e = run_cmd(["git", "-C", repo, "rev-parse", base], 30,
                            sd / "rev", tools=tools)
    base_rev = Path(out).read_text().strip() if code == 0 else "unknown"
    bdir = Path(baseline_root) / base_rev
    carry["base_rev"] = base_rev
    if any(bdir.glob("*.out")):
        return "cached", carry            # unchanged base -> no rebuild, free
    # base advanced: rebuild base clang (the record step snapshots it)
    code, _o, err = run_cmd(["ninja", "-C", build_dir, "clang"],
                            ctx["_timeout_s"], sd / "build", tools=tools)
    if code != 0:
        return "error", dict(carry, exit_code=code, stderr_path=err)
    return "built", carry
