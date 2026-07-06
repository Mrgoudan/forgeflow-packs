# forgeflow-packs

Customization (layer 2) built on the [forgeflow engine](https://github.com/Mrgoudan/forgeflow).
Each directory is a pack: workflows + blocks + prompts + schemas. The
engine repo stays generic; everything domain- or machine-specific lives
here.

## review

Reviews a branch against a base with one agent lens:

```
review.requested {branch, base}
  -> worktree at branch head          (worktree.create)
  -> deterministic diff into cwd      (review.diff, pack block)
  -> agent reads diff + checkout      (agent.run, schema-gated)
  -> claims become findings rows      (review.file_findings, pack block)
```

The agent's output is a claim: findings land in state `found` for a later
triage/evidence stage. A rate-limited model parks the task; nothing hangs.

Try it (needs the engine on PYTHONPATH and the claude CLI):

```bash
ENGINE=~/bsd/forgeflow ./scripts/demo_review.sh
```

Setup for a real repo: `cp review/project.yaml.example review/project.yaml`,
fill `paths.repo`, then:

```bash
python3 -m forgeflow --root ~/ff-review --pack review validate
python3 -m forgeflow --root ~/ff-review --pack review emit review.requested \
    --data '{"branch": "some-branch", "base": "main"}' --drive
```

Forge intake (PR opened -> review.requested) is the next block to add;
the workflow will not change when it arrives.
