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

## embed_server.py — local BERT sidecar

Serves a local sentence-transformers model over the standard /v1/embeddings
protocol so the engine (which never imports ML runtimes) can use it via:

```yaml
models:
  bertish: { base_url: "http://127.0.0.1:7997/v1", model: all-MiniLM-L6-v2 }
```

```bash
python3 scripts/embed_server.py --port 7997     # loads the model once
```

## PR review (the full chain)

```
forge.poll_requested -> pr_intake -> pr.updated -> pr_fetch
   -> review.requested -> review -> review.completed -> pr_report -> PR comment
```

Four workflows chained only by events; `review` itself does not know PRs
exist. Forge access is config: `prs_url`/`comment_url` templates + a
token ref (`FORGE_TOKEN_<REF>` in the 0600 secrets file). `FORGE_WRITE=1`
gates real sends; comments dedup on (target, body sha); polls are
replay-free because pr.updated dedups on (pr, head_sha).

```bash
ENGINE=~/bsd/forgeflow python3 scripts/demo_prreview.py   # fake forge + fake agent
```
