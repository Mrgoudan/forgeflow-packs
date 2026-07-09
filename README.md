# forgeflow-packs

Customization (tiers 2 and 3) built on the [forgeflow engine](https://github.com/Mrgoudan/forgeflow).
The engine stays generic; everything domain- and machine-specific lives
here as **packs** — a pack is one folder of YAML workflows + plugin blocks +
prompts + schemas + config.

## The forgeflow ecosystem (2 repos)

The generic engine, and this pack repo — which carries everything BSC-specific,
**including the knowledge** (vault + data folded in as tracked subfolders):

| repo | holds | GitHub | gitcode |
|---|---|---|---|
| **forgeflow** | the generic, domain-agnostic engine | [`Mrgoudan/forgeflow`](https://github.com/Mrgoudan/forgeflow) | `ziruichen12138/forgeflow` |
| **forgeflow-packs** | *this repo* — BSC packs + `vault/` (static knowledge) + `data/` (DB export) | [`Mrgoudan/forgeflow-packs`](https://github.com/Mrgoudan/forgeflow-packs) | `bisheng_c_language_dep/FORGEFLOW-PACKS` |

`vault/` (static seed: probes, defect catalogue, compiler-internals notes,
method bench) and `data/` (the live `run/` DB's knowledge, exported as chunked
JSONL via `run-bsc.sh export`/`import`) are **tracked subfolders of this repo** —
not separate repos. Secrets live only in `config/` (gitignored) and are never
committed.

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

`run-bsc.sh` defaults `FORGE_WRITE=1` (a live deployment posts for real). Launch
with `FORGE_WRITE=0` for a dry run (egress archives to disk, nothing posted).

`packs/bsc/run-bsc.sh` sources this file so both the GLM env vars and the
forge token reach the daemon from that one place.

## Folder structure

```
forgeflow-packs/
├── README.md                 you are here
├── config/
│   ├── secrets.env           your keys (gitignored, chmod 600)
│   └── secrets.env.example    template
├── run/                      shared runtime: db, worktrees, outputs (gitignored)
├── vault/                    static BSC knowledge: probes, notes, guide, findings (tracked)
├── data/                     DB knowledge export: chunked JSONL (tracked)
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
  [review/RUNBOOK.md](packs/review/RUNBOOK.md).

- **bsc** — the review pipeline specialized for BiSheng C: GLM behind the
  agentic claude CLI (so the `bsc-*` skills load), the in-repo user manual
  as authoritative ground truth (overrides skills; a semantics change
  without a manual update is flagged), and AI review made mandatory — if
  the model breaks down the review re-queues rather than degrading. See
  [bsc/README.md](packs/bsc/README.md).

## Run the tests

```bash
ENGINE=~/bsd/forgeflow python3 -m unittest discover -s tests
```

No model or network needed — a deterministic fake agent and a local fake
forge stand in, so the full pipelines (refutation, severity gate, degraded
mode, manual-wins, the must-update gate, AI-mandatory parking) run offline.

## Getting started (from a fresh clone)

**Prerequisites:** `git`, `python3` (3.8+), a C++ toolchain with `cmake` +
`ninja` (to build the BiSheng C `clang`), and the `claude` CLI on `PATH` (the
agent backend). A GLM endpoint/token and a gitcode token are needed only when
you actually run agents / post — a dry run needs neither.

```bash
# 1. the engine (generic) — clone it to ~/bsd/forgeflow, or point $ENGINE at it
git clone https://github.com/Mrgoudan/forgeflow ~/bsd/forgeflow

# 2. this repo (the packs)
git clone https://github.com/Mrgoudan/forgeflow-packs ~/bsd/forgeflow-packs
cd ~/bsd/forgeflow-packs

# 3. the reviewed compiler — a BiSheng C llvm-project checkout, built once.
#    This is the ONE machine-specific path: edit packs/bsc/project.yaml
#    `paths.repo` if you clone it somewhere other than ~/bsd/llvm-project-dup.
git clone <your BiSheng-C llvm-project>  ~/bsd/llvm-project-dup
#    …then build clang per the BSC project's instructions; forgeflow only needs
#    the resulting  ~/bsd/llvm-project-dup/build/bin/clang  to exist.

# 4. secrets (skip for an offline dry run)
cp config/secrets.env.example config/secrets.env && chmod 600 config/secrets.env
$EDITOR config/secrets.env         # GLM endpoint + token, and the gitcode token

# 5. prove it loads, then launch the control room
./packs/bsc/run-bsc.sh validate                        # every path/ref resolves
FORGE_WRITE=0 ./packs/bsc/run-bsc.sh dash              # dry run → http://127.0.0.1:8787
```

> `validate` checks that `paths.repo` **exists**, so step 3 must be done first.
> Drop `FORGE_WRITE=0` (the launcher defaults it to `1`) once you're ready to
> post for real. To review a *different* gitcode project, change the forge URLs
> + `review_remote` in `packs/bsc/project.yaml`.

**Seed the knowledge (optional):** the campaign's accumulated findings/methods
ship in `data/` (git). `./packs/bsc/run-bsc.sh import` rebuilds a DB from it;
`export` writes it back after a run. Without importing, you start from the
`vault/` seed (`run-bsc.sh port`).
