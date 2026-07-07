# bsc — BiSheng C review pack

Specializes the generic [review](../review) pipeline for BiSheng C:

- **AI = GLM via the agentic claude CLI.** `agents.review`/`refute` use the
  `claude-cli` backend with `model: glm-4.6`, pointed at GLM's
  Anthropic-compatible endpoint through `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN` (forwarded by `env_keys`). Going through the CLI —
  not the text-only openai-compat backend — is what lets the agent load the
  `bsc-*` skills and work in the worktree.

- **Confirmation loads BSC skills + knowledge.** Lens and refute prompts
  tell the agent to invoke the `bsc-*` skills (ownership, borrowing,
  nullability, safe-zone, …) and inject `bsc_notes` (subsystem code notes)
  and `history` (prior defects in the touched files).

- **Manual is ground truth, in the reviewed repo.** It lives at
  `clang/docs/BSC/BiShengCLanguageUserManual.md` INSIDE the repo. The
  `bsc_manual` provider reads it at the branch head (so it reflects the
  manual as updated in this PR) and injects it as **authoritative** —
  it overrides any bsc-* skill that disagrees. If its blob differs from
  `manual_pinned_sha` (the version the skills were validated against) it is
  flagged `CHANGED`: skills are then suspect where they differ.

- **Manual must be updated before review.** The no-AI `bsc.manual_gate`
  step flags any PR that touches a BSC `semantics_prefix`
  (`clang/lib/Sema/BSC`, …) but does NOT touch the manual — a machine
  finding, since the manual is ground truth and must move with semantics.

All three rules are deterministic (git blob hashes + diff name lists) and
covered by `tests/test_bsc.py`.

## Setup

`bsc/project.yaml` is already real (this machine's paths + the gitcode
repo). The only thing missing is your two secret values:

```bash
# put your GLM key + gitcode token into the real (already-created) file
$EDITOR ~/.config/forgeflow/secrets.env      # replace the two REPLACE_* lines

# run via the wrapper (sources secrets so GLM env + forge token both flow)
./bsc/run-bsc.sh validate
./bsc/run-bsc.sh emit forge.poll_requested --data '{}' --drive   # dry run (no FORGE_WRITE)
```

Adjust `bsc/project.yaml` if your reviewed repo lives elsewhere or the
gitcode API path differs.

Everything else (pipeline, egress, degraded mode, tuning) is the review
pack's [RUNBOOK](../review/RUNBOOK.md).

## Proven vs pending

- Proven (no model): manual-wins precedence, the semantics-without-manual
  gate, and the whole pack loads/validates end to end.
- Pending your GLM endpoint + token: the live GLM review. First run with
  `FORGE_WRITE` unset (archive-only) to confirm forge field shapes.
- After changing the manual, re-validate the skills and bump
  `manual_pinned_sha` (get it with
  `git -C <repo> rev-parse HEAD:clang/docs/BSC/BiShengCLanguageUserManual.md`).
