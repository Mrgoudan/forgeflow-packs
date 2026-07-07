"""BSC domain context providers (tier 3). The novel rule here is
manual-precedence: the BiSheng C user manual is ground truth; the bsc-*
skills and code_notes are distilled from it and may lag. So when the agent
confirms a finding it uses the skills — UNLESS the manual changed since the
skills were validated, in which case the changed manual sections are
injected as authoritative and override the skills.

Determinism: "the manual changed" is a git-rev comparison, never a guess.
Same manual rev + same pinned rev => same context.
"""
from __future__ import annotations

from pathlib import Path

from forgeflow.contract import context_provider
from forgeflow.util import run_cmd

_MAX_SECTIONS = 12
_MAX_BYTES = 4000


def _git(env, repo, args, sub):
    out_dir = Path(env.data_dir) / "tasks" / "bsc" / sub
    code, out, err = run_cmd(["git", "-C", repo] + args, 60, out_dir,
                             tools=env.pack.tools)
    return code, Path(out).read_text(errors="replace")


@context_provider("bsc_manual")
def _bsc_manual(env, task, spec):
    """Manual-wins-on-change. Compares the manual repo's current rev to the
    rev the skills were pinned against (pack param `manual_pinned_rev`).

    unchanged -> {status: current} : trust the bsc-* skills as-is.
    changed   -> {status: CHANGED, authoritative: true, changed_sections}:
                 the manual moved; where a skill disagrees, FOLLOW THE MANUAL.
    """
    manual = (env.pack.paths or {}).get("manual")
    if not manual:
        return {}
    pinned = (env.pack.params or {}).get("manual_pinned_rev")
    code, cur = _git(env, manual, ["rev-parse", "HEAD"], "rev")
    if code != 0:
        return {}
    cur = cur.strip()
    if pinned and cur == pinned:
        return {"manual_rev": cur, "status": "current",
                "note": "BSC skills are validated against the current manual "
                        "revision; trust them for confirmation."}

    # changed (or never pinned): surface what moved as authoritative
    if pinned:
        code, names = _git(env, manual,
                           ["diff", "--name-only", "%s..%s" % (pinned, cur)],
                           "diff")
        changed_files = [f for f in names.split() if f.endswith(".md")] if code == 0 else []
    else:
        changed_files = []

    sections = []
    for rel in changed_files[:_MAX_SECTIONS]:
        p = Path(manual) / rel
        if p.is_file():
            text = p.read_text(errors="replace")
            sections.append({"file": rel, "text": text[:_MAX_BYTES]})
    return {"manual_rev": cur, "pinned_rev": pinned, "status": "CHANGED",
            "authoritative": True,
            "note": "The BiSheng C user manual changed since the skills were "
                    "validated. Where a bsc-* skill and these manual sections "
                    "disagree, FOLLOW THE MANUAL — it is authoritative.",
            "changed_sections": sections,
            "changed_files": changed_files}


@context_provider("bsc_notes")
def _bsc_notes(env, task, spec):
    """code_notes for the subsystems this branch touches. subsystem_map
    (pack param) maps a touched path prefix to a note file under
    paths.code_notes. Touched paths come from git, not the model."""
    payload = task.get("payload") or {}
    repo = (env.pack.paths or {}).get("repo")
    notes_dir = (env.pack.paths or {}).get("code_notes")
    smap = (env.pack.params or {}).get("subsystem_map") or {}
    base, branch = payload.get("base"), payload.get("branch")
    if not (repo and notes_dir and base and branch):
        return {}
    code, names = _git(env, repo, ["diff", "--name-only",
                                   "%s...%s" % (base, branch)], "notes-diff")
    if code != 0:
        return {}
    wanted, out = set(), []
    for path in names.split():
        for prefix, note in smap.items():
            if path.startswith(prefix) and note not in wanted:
                wanted.add(note)
                p = Path(notes_dir) / note
                if p.is_file():
                    out.append({"subsystem": prefix, "note": note,
                                "text": p.read_text(errors="replace")[:_MAX_BYTES]})
    return out
