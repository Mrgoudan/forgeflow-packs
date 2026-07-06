# review pack — operator runbook

An industrial PR reviewer on the forgeflow engine. Evidence-first: a no-AI
machine core that runs even with the model down, an agent lens for
semantic defects, and an adversarial refutation pass that drops
speculative findings before anything is posted.

## Pipeline (one review task)

```
review.requested {branch, base, pr?, head_sha?}
  workspace   worktree at the branch head
  diff        unified diff vs base -> ./review.diff
  prescan     [NO AI] pattern grep-rules over added lines -> machine findings
  lens        [AI]    propose candidate findings (context: history, patterns, lessons)
  file        candidates -> findings rows (state 'found')
  refute      [AI]    adversarial: CONFIRM defensible / REJECT speculative
  adjudicate  found -> triaged (confirmed + machine) | rejected (refuted)
  announce    review.completed  ->  pr_report posts triaged findings to the PR
```

Only `triaged` findings reach egress. Rejected findings are archived
(auditable), never posted. Every AI decision is one `runs` row; every
transition is one `transitions` row with its reason as evidence.

## What it reads (accumulated knowledge, all optional)

| context | table | effect |
|---|---|---|
| history | implications ⋈ findings | prior defects in the touched files |
| patterns | patterns (review_lens) | defect-class lenses the model hunts for |
| patterns | patterns (grep_rule) | machine rules the no-AI prescan runs |
| lessons | lessons WHERE task_kind=review | standing review instructions |

Empty tables = empty context, never an error. The reviewer works on day
one and gets sharper as findings/patterns/lessons accumulate.

## Setup

1. `cp review/project.yaml.example review/project.yaml`; fill `paths.repo`
   (a local clone PR heads are fetched into), the forge `prs_url` /
   `comment_url` templates, and `min_severity`.
2. Secrets — `~/.config/forgeflow/secrets.env`, mode 0600:
   `FORGE_TOKEN_<REF>=...` matching `forge_auth.token_ref`.
3. Validate before running anything:
   `python3 -m forgeflow --root ~/ff-review --pack review validate`

## Run

```bash
# one-shot (cron): poll the forge, review every open PR, drain to idle
python3 -m forgeflow --root ~/ff-review --pack review emit \
    forge.poll_requested --data '{}' --drive

# or the daemon (systemd unit in ../systemd/), plus a cron/loop that emits
# forge.poll_requested on an interval
python3 -m forgeflow --root ~/ff-review --pack review run
```

Real forge sends require `FORGE_WRITE=1` in the daemon environment. Without
it every comment is archived (egress row + body on disk), never posted —
the safe default. **Always do the first live poll WITHOUT FORGE_WRITE** and
inspect `data/egress/` to confirm the API field shapes before enabling
sends.

## Observe & recover

```bash
python3 -m forgeflow --root ~/ff-review status        # tasks / parked / findings / events
python3 -m forgeflow --root ~/ff-review trace <id>    # one task's full story
python3 -m forgeflow --root ~/ff-review unpark [id]   # release parked (e.g. model back up)
```

Parked review tasks mean an AI stage hit a limit/backend error — machine
findings for that PR are already filed; unpark when the model returns.

## Degraded mode (proven)

Model completely down: prescan still files machine findings, adjudicate
still triages them; the AI stages park (resumable). The system is never
down, only its yield is reduced. See `scripts/demo_degraded.py`.

## Tuning

- `min_severity` (low|medium|high): floor for what gets POSTED; all
  findings are still filed for audit.
- Add a `patterns` row (grep_rule + review_lens) for each recurring defect
  class — it strengthens both the no-AI prescan and the lens.
- Add `lessons` (task_kind=review) for standing "you missed this" rules.

## Demos (no model cost, fake forge + deterministic agent)

```bash
ENGINE=~/bsd/forgeflow python3 scripts/demo_prreview.py   # full chain + refutation
ENGINE=~/bsd/forgeflow python3 scripts/demo_degraded.py   # model down
```

## Known gaps (not yet built)

- Re-review of the same head_sha isn't guarded by a coverage row yet;
  idempotency currently rests on finding-key and egress body-sha dedup.
- Forge field shapes assume gitee/gitcode v5 (`number/head/base`); confirm
  against your forge on the first dry-run poll.
- Author allowlist / watermarks for who may trigger reviews: pack config
  to add.
