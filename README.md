# forgeflow-packs

Customization (tiers 2 and 3) built on the [forgeflow engine](https://github.com/Mrgoudan/forgeflow).
The engine stays generic; everything domain- and machine-specific lives
here as **packs** — a pack is one folder of YAML workflows + plugin blocks +
prompts + schemas + config.

## Three kinds of thing, kept separate

- **packs** (`packs/`) — pack *definitions*: workflows, blocks, prompts,
  schemas, and `project.yaml`. Version-controlled code.
- **config** (`config/`) — machine-local secrets. `config/secrets.env`
  (gitignored, `chmod 600`) holds your keys; `config/secrets.env.example`
  is the template.
- **runtime** (`run/`) — the shared state root: the SQLite db, worktrees,
  and archived outputs. Gitignored; created on first run. NOT part of any
  pack — every daemon uses this one root.

## Where your API keys go

**One file, `chmod 600`:** `config/secrets.env`

```bash
cp config/secrets.env.example config/secrets.env
$EDITOR config/secrets.env          # replace the two REPLACE_* lines
chmod 600 config/secrets.env
```

It holds:
- `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` — GLM behind the claude CLI
- `FORGE_TOKEN_MAIN` — the forge (gitcode) API token
- `FORGE_WRITE=1` — uncomment only when ready to post to real PRs

`packs/packs/bsc/run-bsc.sh` sources this file so both the GLM env vars and the
forge token reach the daemon from that one place.

## Folder structure

```
forgeflow-packs/
├── README.md                 you are here
├── config/
│   ├── secrets.env           your keys (gitignored, chmod 600)
│   └── secrets.env.example    template
├── run/                      shared runtime: db, worktrees, outputs (gitignored)
│
├── packs/
│   ├── review/               generic PR-review pack (forge-agnostic library)
│   │   ├── RUNBOOK.md         operator guide
│   │   ├── workflows/         intake · prfetch · review · report
│   │   ├── blocks/            reviewblocks.py · forge.py · providers.py
│   │   ├── prompts/           review.md · refute.md
│   │   └── schemas/           review_findings · refute_decisions
│   └── bsc/                   BiSheng C review pack (extends review/)
│       ├── project.yaml       REAL config (tracked): manual, GLM agents
│       ├── README.md          BSC specifics (manual-wins, GLM, gate)
│       ├── run-bsc.sh         launcher: sources secrets, runs the daemon
│       ├── workflows/         bsc_review + the forge workflows
│       ├── blocks/bsc.py      bsc_manual / bsc_notes providers + manual gate
│       └── prompts/           BSC review + refute prompts
│
├── bin/embed_server.py       local embedding sidecar (sentence-transformers)
├── systemd/                  user-unit templates (daemon + sidecar)
├── docs/                     design docs (DESIGN · HUNT · INVOCATION · DATAMODEL)
└── tests/                    unittest suite (fake agent + fake forge; no model cost)
```

**Which pack do I run?** `packs/bsc`. `packs/review` is the generic library
`bsc` reuses its blocks from — you don't run it directly.

## The packs

- **review** — an industrial PR reviewer: a no-AI machine core (pattern
  rules) that runs even with the model down, an agent lens, and an
  adversarial refutation pass that drops speculative findings before
  anything is posted. Only vetted findings reach the PR. See
  [review/RUNBOOK.md](review/RUNBOOK.md).

- **bsc** — the review pipeline specialized for BiSheng C: GLM behind the
  agentic claude CLI (so the `bsc-*` skills load), the in-repo user manual
  as authoritative ground truth (overrides skills; a semantics change
  without a manual update is flagged), and AI review made mandatory — if
  the model breaks down the review re-queues rather than degrading. See
  [bsc/README.md](bsc/README.md).

## Run the tests

```bash
ENGINE=~/bsd/forgeflow python3 -m unittest discover -s tests
```

No model or network needed — a deterministic fake agent and a local fake
forge stand in, so the full pipelines (refutation, severity gate, degraded
mode, manual-wins, the must-update gate, AI-mandatory parking) run offline.

## Quick start (BSC reviewer)

`bsc/project.yaml` is real (not a template). Just add your two secrets:

```bash
$EDITOR ~/.config/forgeflow/secrets.env         # replace the two REPLACE_* lines
./packs/bsc/run-bsc.sh validate                        # prove it loads
./packs/bsc/run-bsc.sh emit forge.poll_requested --data '{}' --drive   # dry run (no FORGE_WRITE)
```
