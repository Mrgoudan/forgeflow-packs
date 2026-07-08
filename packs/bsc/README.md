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

The `vault/bsc/` tree holds **three** kinds of thing, only some of which land
in the db:

1. **Live-executed — never ported.** `probes/` (66 `.cbs` + their
   `.expected.*` oracles) are *run from disk* against clang on every probe
   sweep (review evidence gate + hunt no-AI finder). Delete them and the
   sweep has nothing to run.
2. **Ported seed — a regenerable projection.** `findings.jsonl` and
   `code_notes/` are re-read into the db at every campaign start by
   `bsc.ingest_seed` / `bsc.ingest_notes` (idempotent). The **vault is the
   git-tracked, editable source of truth; the db (`run/*.db`) is a
   disposable cache** you can wipe and rebuild. Delete the vault and the next
   campaign seeds *empty*.
3. **Reference / archive — wired to nothing.** `prompts/`, `claude/`,
   `knowledge/` are the original campaign's artifacts, kept for provenance.
   The pack does **not** read them (our prompts live in `prompts/`, the
   `bsc-*` skills load from the reviewed repo's `.claude`). Prunable.

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
| **the manual** (in the REPO, `clang/docs/.../BiShengCLanguageUserManual.md`) | authoritative correct behavior | **live** (git blob at head) | `bsc_manual` provider — overrides bsc-* skills |
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

## Setup & run

```bash
# 1. secrets (GLM key + gitcode token) — never committed
$EDITOR packs/config/secrets.env

# 2. run via the wrapper (sources secrets so GLM env + forge token flow)
./run-bsc.sh validate
./run-bsc.sh emit forge.poll_requested --data '{}' --drive   # review, dry run (no FORGE_WRITE)
./run-bsc.sh emit hunt.round_requested --data '{"base":"bishengc/15.0.4"}' --drive   # a hunt
```

`FORGE_WRITE=1` gates real posting / PR creation; without it, comments
archive and fixes commit to a local branch only. After editing the manual,
re-validate the skills and bump `manual_pinned_sha`
(`git -C <repo> rev-parse HEAD:clang/docs/BSC/BiShengCLanguageUserManual.md`).

Pipeline/egress/degraded-mode details: the review pack's
[RUNBOOK](../review/RUNBOOK.md).
