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

- **Manual wins on change.** The `bsc_manual` provider compares the user
  manual's git rev to `manual_pinned_rev` (the rev the skills were validated
  against). Unchanged → trust the skills. Changed → the changed manual
  sections are injected as **authoritative** and override any skill that
  disagrees. Deterministic (git revs), proven by
  `scripts/demo_bsc_manual.py`.

## Setup

```bash
cp bsc/project.yaml.example bsc/project.yaml   # fill paths.repo + forge params
# daemon environment (systemd EnvironmentFile, 0600):
export ANTHROPIC_BASE_URL=https://<glm-anthropic-endpoint>
export ANTHROPIC_AUTH_TOKEN=<glm-key>
python3 -m forgeflow --root ~/ff-bsc --pack bsc validate
```

Everything else (pipeline, egress, degraded mode, tuning) is the review
pack's [RUNBOOK](../review/RUNBOOK.md).

## Proven vs pending

- Proven (no model): manual-wins precedence, pack loads/validates end to end.
- Pending your GLM endpoint + token: the live GLM review. Do the first run
  with `FORGE_WRITE` unset (archive-only) to confirm before posting.
- After changing the manual, re-validate the skills and bump
  `manual_pinned_rev`.
