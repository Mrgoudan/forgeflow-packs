# bsc — BiSheng C pack (review · hunt · fix)

The **invocation** that binds the generic engine + capability packs to this
deployment. `project.yaml` is the only file with BSC-and-this-machine
specifics; it composes three generic packs:

| capability | pack | what it does | entry event |
|---|---|---|---|
| **review** | [`../review`](../review) | review an open PR against BSC ground truth, post a verdict | `forge.poll_requested` |
| **hunt** | [`../hunt`](../hunt) | autonomously mine the compiler for new defects | `hunt.round_requested` |
| **fix** | [`../fix`](../fix) | turn a confirmed finding into a verified patch + PR | `fix.requested` |

Nothing decides *between* them — you emit the entry event for the one you
want (`run-bsc.sh emit <event>`); the engine routes it by each workflow's
`consumes:`. Same engine, same db, same probe oracle.

---

## Why the vault is still here (it is NOT fully "ported")

After pruning, `vault/bsc/` holds **only what's used** — two kinds of thing,
both needed:

1. **Live-executed — never ported.** `probes/` (66 `.cbs` + their
   `.expected.*` oracles) are *run from disk* against clang on every probe
   sweep (review evidence gate + hunt no-AI finder). Delete them and the
   sweep has nothing to run.
2. **Source — a regenerable projection.** `findings.jsonl`, `code_notes/`,
   and `knowledge/KNOWLEDGE.md` are read into the db (or injected) at every
   campaign start (`bsc.ingest_seed`/`bsc.ingest_notes`/`bsc_compiler_guide`).
   The **vault is the git-tracked source of truth; the db (`run/*.db`) is a
   disposable cache** you can wipe and rebuild. Delete the vault and the next
   campaign seeds *empty*.

The former archive (language references, `bishengc_rules.json`, the training
dataset, `TRIAGE.md`, `prompts/`, `claude/`) was **removed** — each was wired
to nothing: the language refs would be a *competing* ground truth to the
in-repo manual, `rules.json` had no consumer, the dataset is training data
(not runtime context), `TRIAGE.md`'s canonical form is `findings.jsonl`, and
the prompts/agents were already adapted into `prompts/` or load from the
repo's `.claude`. Recoverable from vault git history if ever wanted.

> **Asymmetry to know:** runtime-GROWN knowledge (new findings from a hunt,
> Oracle-Scout methods, explorer readings) lives ONLY in the db, not written
> back to the vault. So `db = vault seed + runtime growth`. Wipe the db and
> you keep the seed but lose the growth. A "harvest back to vault" step would
> close that loop — not built.

---

## Data catalogue — what each source holds, and who reads it

| source | holds | ported? | consumed by |
|---|---|---|---|
| `vault/bsc/probes/*.cbs` (+`.expected.*`) | 66 differential probes + oracle outputs | **live** (run from disk) | `hunt.probe_sweep` → review evidence gate + hunt no-AI finder |
| `vault/bsc/findings.jsonl` | 142 known defects (the dedup catalogue) | → `findings` | explorer (don't re-file), review/fix history |
| `vault/bsc/code_notes/_playbook.md` | C1–C12 defect classes | → `patterns` | explorer/reviewer/scout classification lens |
| `vault/bsc/code_notes/_methods.md` | 20 detection methods + bandit priors | → `methods` | the hunt bandit (`hunt.pick_region` dispatch) |
| `vault/bsc/code_notes/_chains.md` | A–Z call-chain surfaces | → `chains` | explorer Mode-2 (`hunt_region`) |
| `vault/bsc/code_notes/INDEX.md` + subsystem notes | per-file compiler-internals notes | → `readings` (`bsc.ingest_notes`) | explorer (`hunt_region`) + reviewer (`bsc_notes`) |
| `vault/bsc/knowledge/KNOWLEDGE.md` | how to edit each compiler subsystem + recurring change shapes | **live** (read at run) | `bsc_compiler_guide` provider → fixer + explorer |
| **the manual** (in the REPO, `clang/docs/.../BiShengCLanguageUserManual.md`) | authoritative correct behavior (LANGUAGE) | **live** (git blob at head) | `bsc_manual` provider — overrides bsc-* skills |
| db `regions` | file-level explore surface (dirs + `ENABLE_BSC` grep) | grown at runtime | `hunt.pick_region` |
| db `findings/patterns/methods/chains/readings` | seed **+ runtime growth** | idempotent re-seed | every workflow |

---

## Task catalogue — workflow → trigger → data it reads → prompt → output

| workflow | trigger (consumes) | key data / context | agent · prompt | output |
|---|---|---|---|---|
| **review** (`bsc_review`) | `review.requested` | `probe_results`, `bsc_manual`, `bsc_notes`, `patterns`, `history`, PR `payload` | `review` · [`prompts/review.md`](prompts/review.md) → `refute` · [`prompts/refute.md`](prompts/refute.md) | posts a verdict comment |
| **hunt round** (`hunt_round`) | `hunt.round_requested` | ingests the vault; seeds regions; builds base; runs the probe sweep | — (deterministic) | kicks `hunt.explore_requested` |
| **explore** (`hunt_explore`) | `hunt.explore_requested` | `hunt_region` (leased file + readings/chains), `hunt_method` (dispatched arm), `bsc_manual`, `patterns` | `explore` · [`prompts/explorer.md`](prompts/explorer.md) | a candidate → verified → `findings` |
| **exploit** (`hunt_exploit`) | `hunt.pattern_confirmed` | the confirmed pattern + finding (`payload`), `bsc_manual` | `exploit` · [`prompts/exploiter.md`](prompts/exploiter.md) | pattern variants → `findings` |
| **scout** (`hunt_scout`) | `hunt.scout_requested` (on saturation) | `hunt_arsenal` (active+exhausted bench + confirmed findings), `patterns`, `bsc_manual` | `scout` · [`prompts/scout.md`](prompts/scout.md) | new `methods` → reopens explore |
| **fix** (`fix_finding`) | `fix.requested` | `fix_target` (the finding's evidence + repro), `bsc_manual` | `fix` · [`prompts/fixer.md`](prompts/fixer.md) | verified patch → PR (`pr_open`) |

All six agents run **GLM-5.2 behind the claude-cli backend** (so the `bsc-*`
skills load and the agent can work in a worktree). Each output is validated
against a schema in [`../hunt/schemas`](../hunt/schemas) /
[`../review/schemas`](../review/schemas) / [`../fix/schemas`](../fix/schemas).

---

## Ground-truth rules (deterministic, no model)

- **Manual wins on change.** `bsc_manual` reads the manual at the branch head
  and injects it as *authoritative*; it overrides any `bsc-*` skill. If its
  blob differs from `manual_pinned_sha` it is flagged `CHANGED` (skills
  suspect where they differ).
- **Manual must move with semantics.** A PR touching a `semantics_prefix`
  but not the manual is a machine finding.
- **Evidence gate.** The probe sweep runs a PR-built clang vs a cached base
  baseline; behavior *flips* are evidence for the AI, not findings.

Covered by [`tests/test_bsc.py`](../../tests/test_bsc.py).

---

## Run

Everything launches through `run-bsc.sh` (a thin wrapper that sources
`packs/config/secrets.env` so the GLM env + gitcode token flow, unsets the
proxy for the domestic endpoints, and — for a **live deployment — exports
`FORGE_WRITE=1` so egress posts for real**).

```bash
# 0. secrets (GLM key + gitcode token) — never committed
$EDITOR packs/config/secrets.env
./run-bsc.sh validate                  # config check: every workflow total, refs resolve

# --- run the daemon (pick ONE; one daemon per state root) ---
./run-bsc.sh dash                      # control room: daemon + web UI → http://127.0.0.1:8787
./run-bsc.sh dash --port 8787          # (explicit port)
./run-bsc.sh run                       # headless daemon (no UI)

# --- dry run (archive to disk, DON'T post to the forge) ---
FORGE_WRITE=0 ./run-bsc.sh dash

# --- one-shot: fire a single entry event and drive it to completion ---
./run-bsc.sh emit hunt.round_requested --data '{"base":"bishengc/15.0.4"}' --drive
./run-bsc.sh emit forge.poll_requested --data '{}' --drive     # poll + review open PRs
./run-bsc.sh port                      # one-time vault → db knowledge seed
```

### Backup / transfer (DB ↔ git)

The **DB is the living source of truth**; `export` projects its knowledge into a
small, diffable SQL text file (`data/forgeflow.knowledge.sql`, ~660 KB) that
lives in its own `data/` git repo (like `vault/`). Operational/regenerable
tables (events/tasks/runs/embeddings) are omitted; the engine re-creates them
empty on open.

```bash
./run-bsc.sh export     # DB knowledge  -> data/forgeflow.knowledge.sql (commit it)
./run-bsc.sh import     # rebuild run/state/forgeflow.db from that dump (--force to overwrite)
```

Transfer = commit + push `data/` on one machine, clone + `import` on the other.
**`data/` is PRIVATE** — it holds security findings + reviewed-code snippets (no
secrets: verified the GLM key / forge token never appear in it). Encrypt before
pushing to any remote (git-crypt or age).

Stop the daemon with `fuser -k 8787/tcp` (kills by port). To halt work without
killing, hit **⏸ pause** in the dashboard (or set `paused=1` in `dash_control`).

**Triggering model:** review + fix **auto-trigger** (review polls for open PRs;
fix fires on each new triaged finding); hunt is **manual** (`▶ run` / `emit`)
but self-sustains once started. Parked tasks auto-recover on a per-class
cadence (`agent_limit` probes GLM every 30 min and restarts when it answers;
`forge_auth` never auto-recovers — fix the token, then unpark).

## Control room (the dashboard)

`./run-bsc.sh dash` is a self-contained (stdlib-only) local web UI that **is**
the daemon: a gated claim→execute loop runs behind it and obeys the controls
you click. It replaces `run` — don't run both.

- **Stats** — tasks / findings / methods / regions / PRs / hunt round, live.
- **Controls** — per capability (bug hunt · review · fix): **▶ run** (emits
  the trigger), **enable/disable** (gates that capability's tasks), plus a
  global **⏸ pause all**.
- **Queue** — every active task with its kind, state, current step; click a
  row to jump to that block and see its message.
- **Workflow block-maps** — each workflow drawn as its blocks; a pulse marks
  the block **running right now**. **Click any block** for a drawer: its
  outcomes, injected context, params, the **agent prompt**, output schema, and
  recent runs.

> `FORGE_WRITE` (default **1** via `run-bsc.sh`) gates real posting / PR
> creation; with `FORGE_WRITE=0` comments archive and fixes commit to a local
> branch only. After editing the manual, re-validate the skills and bump
> `manual_pinned_sha`
> (`git -C <repo> rev-parse HEAD:clang/docs/BSC/BiShengCLanguageUserManual.md`).

Pipeline/egress/degraded-mode details: the review pack's
[RUNBOOK](../review/RUNBOOK.md).
