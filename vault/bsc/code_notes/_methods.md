# Oracle ledger — method-diversity bandit state (SSOT)

This file is to **detection methods (oracles)** what `_playbook.md` is to defect
classes and `_chains.md` is to call-chains: the single source of truth for *how
the campaign decides something is a bug*, and how well each way is currently
paying.

The campaign's original axes were **mode** (function-read vs chain-trace) and
**stance** (explore vs exploit). Both share ONE oracle — "a human reads the
analyzer, predicts where an invariant is wrong, writes a probe to confirm." That
oracle saturates: widening the *target* (new functions, new chains) re-samples
the same hypothesis distribution. The third axis is the **oracle itself** — a
new *way to detect*, ideally one that flags bugs nobody predicted. This ledger
tracks the bench of oracles and rotates the campaign onto whichever is most
rewarding right now, so no single method is ever repeated to exhaustion.

## How this ledger is driven (proactive bandit, NOT saturation-gated)

- The **Oracle-Scout** (`.claude/agents/bsc-oracle-scout.md`) runs **continuously**
  on an adaptive budget slice — it does NOT wait for saturation. Each round it
  *invents* novel oracle candidates (open-ended; there is **no preset taxonomy**
  it must draw from) and hands the Conductor a trial recipe per candidate.
- The Conductor spawns a few **Oracle-Trial** agents
  (`.claude/agents/bsc-oracle-trial.md`) in parallel — each runs **in a git
  worktree sandbox, report-only, never files** — to cheaply score one candidate.
- The Conductor folds each trial's reward estimate into the table below, keeps
  the bench ranked, and **rotates the deployed oracle** the moment a bench arm's
  estimate beats the deployed arm's live yield.

Saturation is therefore only the **demotion signal** for the deployed arm — the
bench is always pre-ranked, so rotation has zero latency. See
`docs/AGENTS.md` § "Method-diversity track" for the full protocol.

## Reward & rotation mechanics

- **Reward** per oracle = `genuine_distinct_finds / cost_tokens` (finds that
  spot-check as real bugs, are in-scope, and do NOT dup an existing `Fxx`).
  A trial returns an *estimate* over a small input slice; the Conductor keeps a
  running estimate across that oracle's trials.
- **Exploration guarantee**: every scout round must propose ≥1 brand-new
  (never-trialed) oracle, so the bench never ossifies — this is the structural
  answer to "repeating a method → diminishing returns."
- **Rotation** (with hysteresis to avoid thrash): promote bench-best → deployed
  when `best_bench_reward > 1.3 × deployed_live_reward` sustained over a window,
  OR when the deployed arm returns 0 distinct finds for K consecutive cycles.
  Demote the old deployed arm back to the bench (it may recover later — the
  codebase changes under it).
- **Adaptive budget slice** (the campaign's decision):
  - *Bootstrapping* (< 3 scored oracles on the bench): scout cadence ≈ every 3
    Conductor cycles, ~30 % of budget — fill the bench fast.
  - *Steady* (≥ 3 scored oracles): cadence ≈ every 6–8 cycles, ~15 % of budget.

## Dedup vocabulary (tag only — NOT a generator)

When recording an oracle that has been trialed, optionally tag it with this
4-tuple so the scout can tell whether a new idea is genuinely novel or a
re-skin of something already run. The scout MUST NOT limit its invention to
these dimensions — they exist only to dedup the ledger.

`⟨ input-source · perturbation · comparison · verdict-extractor ⟩`
- input-source: `cases/` · `repro/` · grammar-sampled · coverage-gap file · …
- perturbation: identity · semantics-preserving rewrite · compiler-config ·
  compiler-version · rewrite-roundtrip · …
- comparison: verdict-equality · runtime-equality · coverage-delta · …
- verdict-extractor: compile pass/fail · specific diagnostic · valgrind · …

## Status table

Status ∈ {deployed, bench, demoted, dead}. `reward` is finds/100k-tok, `—` if
not yet trialed. Keep newest trials' numbers; the Conductor edits this table.

| id | one-line description | status | trials | inputs | genuine | false-alarm | reward | last-trialed | dedup-tag |
|----|----------------------|--------|--------|--------|---------|-------------|--------|--------------|-----------|
| `src-reading` | invariant-driven white-box read + hand probe (campaign default) | demoted | — | — | — | — | 0 (recent) | rotated-out r8 | `⟨analyzer-src · identity · human-predicts · n/a⟩` |
| `diff-vs-c` | same construct in `.c` vs `.cbs`, verdict-equality | demoted | — | — | — | — | — | ad-hoc | `⟨corpus · language-swap · verdict-eq · compile⟩` |
| `rewrite-roundtrip` | `-rewrite-bsc` output inspection / recompile (found F09) | demoted | — | — | — | — | — | ad-hoc | `⟨corpus · rewrite-roundtrip · verdict-eq · compile⟩` |
| `valgrind-on-safe` | runtime valgrind on compile-time-"safe" programs (F02/F05/F09) | demoted | — | — | — | — | — | ad-hoc | `⟨safe-subset · identity · runtime-eq · valgrind⟩` |

> The four rows above are the **already-in-repertoire** oracles, recorded so the
> scout dedups against them. Their reward was never formally measured (they were
> used ad hoc); `src-reading` is the one that is currently saturating. The
> **bench below is empty — the scout fills it by invention.**

### Bench (scout-proposed, awaiting / holding trial scores)

From round 1 (2026-06-05). All brand-new, in-scope, deduped vs the four
in-repertoire oracles. `reward` = `—` until trialed. Full trial recipes in the
round log below.

| id | one-line | reaches class | rank | status | reward |
|----|----------|---------------|------|--------|--------|
| `position-equivalence-metamorphic` | move a known-REJECTED read of value V into every other AST position; any flip to ACCEPT = position-coverage hole | C2/C3 (F88/F90/F91 family) | 1 | demoted | 0 (regr-HIGH) |
| `narrowing-invalidation-fuzz` | splice a mutation between a narrow and a safe deref; inline-write twin is the ground-truth oracle | C7 (F84/85/87/89 family) | 2 | demoted | 0 (regr-HIGH) |
| `flag-monotonicity-differential` | more-checks config must never ACCEPT what less-checks REJECTED; non-monotone pair = flag-gating bug | flag/annotation suppression (F86) | 3 | demoted | 0 (regr-MED) |
| `double-free-conservation-runtime` | alloc==free counted conservation law on analyzer-ACCEPTED _Owned code | silent path-dependent leak/double-free (F02/F20/F22/F91) | 4 | demoted | 0 (regr-HIGH) |
| `merge-asymmetry-branch-join-generator` | one-sided fact at a CFG join must be DROPPED by the meet; both/neither-sided twins pin truth | C5 merge asymmetric-key-absence (mergeVD/FP/DPVD:844, OwnershipImpl::merge, InitAnalysis::merge) | 1 (r3) | demoted | 0 (regr-MED) |
| `loop-fixpoint-iteration-divergence` | loop back-edge re-entry vs hand-unrolled 2-iter twin; fixpoint must propagate body degradation | C5/C7 across iterations | 2 (r3) | demoted | 0 (regr-MED) |
| `interprocedural-owned-handoff-contract-diff` | call-site summary vs hand-inlined single-call by-value helper body | inter-proc ownership handoff (Mode-2) | 3 (r3) | demoted | 0 (regr-LOW; model proven sound) |
| `compound-assign-nullable-field-roundtrip` | identity compound-assign on nullable FIELD launders FieldPath narrowing vs no-op twin | C7 field-keyed launder (vs F48/F50 var-keyed) | 4 (r3) | bench (untrialed) | — |
| `rewriter-runtime-equivalence` | run .cbs vs its -rewrite-bsc C output; compare RUNTIME observables (stdout/exit/alloc-ledger/valgrind) | **subsystem: RewriteBSC** semantic divergence | 1 (r5) | demoted | 0 (rewriter proven runtime-faithful on in-scope) |
| `opt-level-output-invariance-ubfree` | -O0 vs -O1/2/3 on UB-free programs; divergence = backend miscompile of a BSC lowering | **subsystem: BSCIRBuilder+LLVM** codegen | 2 (r5) | bench | — |
| `borrow-accept-runtime-alias-witness` | runtime alias witness through two ACCEPTED non-union borrows; sentinel laundering = borrow FN | **subsystem: BSCBorrowChecker** region/path-identity | 3 (r5) | bench | — |
| `accepted-twin-precision-metamorphic` | ACCEPTED Positive/-corpus seed + safety-preserving subtlety rewrite must STAY accept; flip to REJECT = FP | **FALSE-POSITIVE/precision** (inverse of position-equiv) | 1 (r7) | demoted | 0 (precision robust on idiom rewrites) |
| `accept-reject-boundary-bisection` | sweep a scalar (field-nest/re-borrow/lifetime/FieldPath depth, known NO-limit); any finite flip = off-by-one/cap | **boundary/off-by-one** | 2 (r7) | **DEPLOYED** | **5.5 ✅ depth=10 cap → F96 (MED silent leak FN + depth-11 free FP); now top arm after complexity retraction** |
| `corpus-shrink-equivalence-fp` | delete a provably-irrelevant stmt from an ACCEPTED program; verdict flip = right-verdict-wrong-reason | **internal-inconsistency** | 3 (r7) | bench | — |
| `ast-dump-move-point-consistency` | diff the BSCIR dump's move/borrow/narrow POINT across equivalent programs | **internal-state** (high setup) | 4 (r7) | demoted | 0 (internal state consistent) |
| `analyzer-ice-precondition-fuzz` | drive in-scope borrow/owned code at llvm_unreachable sites; compiler SELF-CRASH = bug | **compiler ICE/robustness** (8 sites: BorrowCk:908/937, Ownership:482-589) | 1 (r10) | demoted | 0 (8 sites PROVEN unreachable dead-code guards; 1357 inputs no crash) |
| `compile-determinism-idempotence` | same source+flags compiled twice must give byte-identical diagnostics; rewrite idempotence | **intra-binary reproducibility** | 2 (r10) | demoted | 0 (compiler deterministic; rewrite idempotent mod cosmetic ws) |
| `diagnostic-code-spec-conformance` | minimal trigger per documented error code → must emit right code/location/entity | **diagnostic-vs-spec** (133-code catalog) | 3 (r10) | **workable** | **5.0 ✅ owned-binOp diag fires TWICE** |
| `analyzer-complexity-blowup-timing` | scale a param, measure compiler wall-clock; super-linear = compile-time DoS | **compiler resource/time** | 4 (r10) | demoted | 0 ❌ O(N²) find RETRACTED — does NOT reproduce (3 reconstructions flat); was sub-second measurement noise. Re-validation caught it. |
| `spec-rule-must-reject` | **INVERSE-seed of `diagnostic-code-spec-conformance` (line 109)**: enumerate the *manual's* normative rejections (`// error:` examples; "X 属性 / 只能用于… / 不能…" subject+context restrictions) — NOT the compiler's `err_` catalog — build the minimal VIOLATING probe and assert HARD ERROR (rc≠0). A spec-mandated rejection emitted as a *warning* or *silently accepted* = hit. Free cross-check: grep `err_*` vs `warn_*`/`-Wignored-attributes` per rule. | **spec-rule→severity** (warn-instead-of-error · spec-error-with-no-`err_`-code · silent-accept) — precisely the gap line-109 can't reach (it seeds from codes that already exist) | 1 (r11) | **workable** | **F113 (MED) FILED — 1 genuine find. FULL SWEEP 2026-06-24 (spec_verifier.py): 75 manual `// error:` blocks across all 6 in-scope chapters → COMPLIANT (every ACCEPT hit was a commented-out violation / intended `// ok` control, all uncomment-verified to ERROR) + placement matrix (_Owned×5, _Borrow×2, _Nonnull/_Nullable×4, __assume_init×2 all hard-error). Manual is HIGHLY conformant; F113 (ensure_init-family non-param = warn-not-error) is the lone gap. Low false-alarm (caught+rejected ~6 spurious commented-line hits).** Spec-text-seeded; reusable tool `sem_tests/scripts/spec_verifier.py`. SEED (user, 2026-06-23): `ensure_init` / `ensure_init_if_ret` on a NON-PARAM var → `-Wignored-attributes` WARN + silent attribute-drop (rc=0); manual frames it a parameter-attribute (是一个参数属性) and every *sibling* rule is `err_ensure_init_*` → misplacement should hard-error too. In scope (BSC init attribute). |

> **All four ROUND-1 arms DEMOTED, not DEAD.** Each is a *sound, low-false-alarm detector* that
> reliably surfaces its bug family (SIGSEGV/leak-confirmed) — it scored 0 only
> because its inputs (existing repros/corpus) make it re-derive already-FILED
> bugs. Rewards are **non-stationary**: once the fork fixes F87/F90/F91/etc.,
> these become high-value **regression** oracles. Re-trial then. The lesson (see
> Round 2 log) is that the input source, not the detector, is the lever.

## Round log

Append one block per scout round: candidates proposed, trials run, rewards,
rotation decision. Newest first.

<!-- ROUND-LOG-START -->

### Round 13 — 2026-06-24 — r11 BIDIRECTIONAL extension (main thread) — Direction-2 first hit

The r11 `spec-rule-must-reject` oracle was extended to its COMPLEMENT (user framing:
"test !A if spec says A is forbidden, **or vice versa**"). Two directions over the
manual now:
- **D1 must-reject** (existing): spec says A forbidden → probe A → HIT if rc=0 (accepts-illegal, FN).
- **D2 must-accept** (`spec_verifier.py --accept`, NEW): spec marks A legal (`// ok`/`// 正确`) →
  probe A → HIT if rejected with a BSC-safety diag (rejects-legal, FP). Filters snippet-context
  noise (SNIPPET_NOISE) so incomplete examples don't masquerade as over-rejections.

D2 sweep over 5 in-scope chapter-3 files: **14 accepted-ok · 7 snippet-noise · 2 FP-CAND**
→ after scope filter, **1 genuine: F114** (borrow-of-function, `2-borrowing.md:1052` rule 16 —
`void (*_Borrow const p)()=&_Const f` rejected by `err_owned_qualifier_non_pointer` +
`err_mut_or_const_expr_func`/SemaExpr.cpp:15343,15374; deliberate checks → real spec↔impl
conformance FP, MEDIUM, inverse of F113). Other cand = `_Trait` (OOS). Repro F114; pending
user file/no-file. D2 is the FP-direction the campaign historically under-ran (combinatorial
gen finds FPs but not *spec-anchored* ones) — keep both directions when re-running r11.

### Round 12 — 2026-06-08 — bsc-oracle-trial ×2 (r10 ranks 3-4) — 🎯 TARGET MET: 3 WORKABLE

| arm | n | genuine | reward | result |
|-----|---|---------|--------|--------|
| **diagnostic-code-spec-conformance** | 30 | **1** | **5.0** | **works=TRUE ✅** |
| **analyzer-complexity-blowup-timing** | 24 | **1** | **6.67** | **works=TRUE ✅** |

**TWO MORE WINS — the "totally different" pivot delivered.** Both observe things
no prior oracle did, and both found NEW, in-scope, distinct bugs:
- **diagnostic-vs-spec**: the owned-pointer binary-op operand error is emitted
  TWICE at the identical caret for one `_Owned* + _Owned*` expression.
  **Conductor differential CONFIRMS BSC-specific**: plain-C `int*+int*` = 1 error,
  BSC non-owned `int*+int*` = 1 error, BSC `_Owned*+_Owned*` = **2 errors** — so
  the duplicate fires only on the _Owned path (DiagnoseOwnedPointerBinaryOp adds a
  redundant emission). MED, user-facing, in-scope, not in bug_log.
  sample: `/tmp/oracle_trial.htITSF/sample_D1.cbs`. (4 sibling candidates correctly
  discarded as mode-gated / sibling-code / ambiguous.)
- **compiler-time**: claimed O(N²) BSC-analyzer excess on chained `_Borrow`
  reborrows / branchy borrow CFGs (doubling-ratio 4.3-4.5× at N=256→512).
  **⚠️ RETRACTED 2026-06-08 (see _probed.md):** Conductor re-validation with THREE
  independent reconstructions (chained reborrows, branchy if-chains, N-loans×N-
  accesses) at N up to 512-800 is FLAT (~0.03s, ratios ~1.0-1.5). The signal was
  sub-second measurement NOISE (wall-clock ratios unreliable at ~0.1s; C-baseline
  subtraction amplifies it). NOT a real bug; NOT filed. The complexity oracle's
  sole find was spurious → oracle DEMOTED.

**WORKABLE COUNT: 2 of 3 (complexity retracted on re-validation).** Two confirmed
workable oracles, each a DISTINCT observable + distinct REPRODUCIBLE bug:
1. `accept-reject-boundary-bisection` (boundary) → depth=10 cap (F96, MED, reproduces) — now DEPLOYED.
2. `diagnostic-code-spec-conformance` (diag-vs-spec) → double owned-binOp diag (F94, MED, differential-confirmed).
3. ~~`analyzer-complexity-blowup-timing`~~ — RETRACTED (sub-second timing noise).

Rotation: `accept-reject-boundary-bisection` (reward 5.5) is the DEPLOYED arm
(complexity demoted to 0 after retraction). **Lesson: a timing-based oracle needs
warm-up + multiple samples + absolute-time floors; sub-second wall-clock ratios
are noise. Re-validation before filing is what caught the spurious find** — the
same F49 discipline that catches dup/false bugs. The 2 reproducible MED finds
(F94, F96) + the loop-found F95 (HIGH) are the genuine fileable outputs.

### Round 11 — 2026-06-08 — bsc-oracle-trial ×2 (r10 ranks 1-2, the orthogonal bets)

| arm | n | genuine | result |
|-----|---|---------|--------|
| analyzer-ice-precondition-fuzz | 1357 | 0 | works=false — 29 targeted + 1328-corpus, ZERO crashes. Trial PROVED all 8 llvm_unreachable sites dead-code: "Unexpected branch" needs source∉{OPS,S} (recursion never yields); free-region guards need FreeRV as constraint `sub` but it only appears as `sup`. Defensive asserts, genuinely unreachable. |
| compile-determinism-idempotence | 30 | 0 | works=false — compiler fully deterministic (30×5 byte-stable); 11 idempotence diffs all = one cosmetic whitespace artifact (`_Safe` strip leaves leading space), semantically identical. |

Two more robust-NEGATIVES on the "totally different" axis: no reachable ICE,
compiler is deterministic. The pattern across 13 trials is now overwhelming —
**the BSC analyzer/compiler is genuinely ROBUST** across soundness, precision,
internal-state, ICE, and determinism. The lone workable oracle (boundary-sweep,
1 edge-case FP) remains the only new-distinct find. Remaining untrialed orthogonal
arms: r10-rank3 `diagnostic-code-spec-conformance` (wrong-code/loc/missing — a
different CORRECTNESS class), r10-rank4 `analyzer-complexity-blowup-timing`
(compile-time DoS). Trialing both next — best remaining shots at workable #2.

### Round 10 — 2026-06-08 — bsc-oracle-scout (steer: TOTALLY DIFFERENT, orthogonal observable)

User directive: "something totally different, [not] pushing for variant[s]." Steered
OFF the boundary oracle and every tried family onto observables in NONE of the
tried buckets (compile verdict / runtime / rewrite / -O / BSCIR-dump / scalar
sweep). Scout's load-bearing discovery: build has `LLVM_ENABLE_ASSERTIONS=OFF`
(CMakeCache:902) → bare `assert()` compiled out, but `llvm_unreachable` still
traps. 4 brand-new arms, on bench:

1. **`analyzer-ice-precondition-fuzz`** (rank 1, the bet): drive in-scope
   borrow/owned programs at 8 located `llvm_unreachable` sites —
   BSCBorrowChecker.cpp:908/937 ("Free region should not grow anymore!") and
   BSCOwnership.cpp:482/496/511/561/574/589 ("Unexpected branch", SAllOwnedFields
   field-name construction). Observable = COMPILER self-crash (signal exit /
   "UNREACHABLE executed" / stack dump), not a verdict. Unambiguous, ~30
   syntax-only compiles, zero false-alarm subtlety. A crash on valid in-scope
   input = HIGH-severity (unlike the depth-11 edge FP).
2. **`compile-determinism-idempotence`** (rank 2): same source+flags compiled 5×
   must give byte-identical (normalized) diagnostics; `rewrite(rewrite(P))==
   rewrite(P)`. Observable = intra-binary reproducibility. ~150 cheap compiles.
3. **`diagnostic-code-spec-conformance`** (rank 3): minimal canonical trigger per
   in-scope error code in DiagnosticBSCSemaKinds.td (133 codes); compiler must
   emit THAT code at the right caret naming the right entity. Observable =
   diagnostic-vs-spec. Catches wrong-code/loc/entity + silent-missing.
4. **`analyzer-complexity-blowup-timing`** (rank 4): scale a structural param
   (borrow-chain/owned-field/branch/FieldPath depth), measure compiler wall-clock
   minus a plain-C baseline; super-linear = compile-time DoS. Observable = time.

**Next**: trial ranks 1 (ICE) + 2 (determinism) in parallel — cheapest + most
orthogonal; the ICE arm could yield a high-severity crash (workable #2).

### Round 9 — 2026-06-05 — bsc-oracle-trial ×2 (r7 ranks 3-4) + bug dup-validation

| arm | n | genuine | result |
|-----|---|---------|--------|
| corpus-shrink-equivalence-fp | 26 | 0 | works=false — analysis robustly delete-invariant; guards genuinely consulted (ablation→REJECT as expected). No wrong-reason acceptance. |
| ast-dump-move-point-consistency | 13 | 0 | works=false — BSCIR dump is stable/readable, but all 6 structural diffs normalized away (expected consequences of the transform); internal state CONSISTENT. |

**Bug dup-validation (Conductor, the F49 discipline).** The depth-11 find
reproduces (`error: invalid cast … uninit value: s.a.…a.p`). Dup-grep hit
bug_log:275 (`initS`, `depth==10`) inside **F19** — so I verified distinctness by
reading F19 + initS/initOPS (:459-592):
- F19 = `checkSFieldAssign` doesn't clear `Uninitialized` after a field-assign;
  its `depth==10` is just the INITIAL arg. Fix = update SStatus in checkSFieldAssign.
- Ours = `depth` DECREMENTS (`initS(…,depth-1,…)` :514/:592), bottoms out at 0 →
  `depth=10` is a recursion CAP; fields nested >10 deep never enter
  `SAllOwnedFields` → safe_free FP. Fix = the depth limit itself.
→ **DISTINCT root cause, distinct fix.** But severity **LOW** (depth-11 struct
nesting is an edge case, not a canonical idiom) → per policy, do NOT file solo.
Value is as proof that the **limit-sweep oracle family** is productive.

**Workable count: 1 of 3.** Subtle-observable scan so far: boundary/limit-sweep =
PRODUCTIVE (1 find); precision-metamorphic / delete-invariance / internal-state =
all ROBUST (clean negatives — the analyzer holds). Productive vein is
boundary/limit. Next: scout DISTINCT subtle-bug detection principles (the win
counts as one workable oracle; need 2 more DISTINCT ones, not more bugs from the
same boundary oracle) — e.g. analyzer magic-constant/resource-limit sweep,
diagnostic-correctness, sound-preserving type-qualifier perturbation,
feature-interaction.

### Round 8 — 2026-06-05 — bsc-oracle-trial ×2 (FIRST WORKABLE ORACLE + FIRST ROTATION)

| arm | n | discrep | genuine | reward | result |
|-----|---|---------|---------|--------|--------|
| accepted-twin-precision-metamorphic | 63 | 3 | 0 | 0 | works=false; 60 valid idiom-rewrites all stayed ACCEPT → analyzer precision robust (3 discrep were false-alarms: transform mis-applied to _Borrow-field struct / ensure_init mis-scoped) |
| **accept-reject-boundary-bisection** | 37 | 1 | **1** | **5.5** | **works=TRUE ✅** |

**THE WIN.** `accept-reject-boundary-bisection` found a NEW, in-scope,
MEDIUM **false-positive** bug — the first new-distinct find of the whole track:
> Nested struct with an `_Owned` field at each level → ACCEPT at depth ≤10,
> spurious REJECT at depth **11**. Root cause: `initS()/initOPS()` in
> `BSCOwnership.cpp` default `depth=10`, return early at `depth==0`; fields nested
> deeper than 10 are never added to `SAllOwnedFields[VD]`, so `safe_free()`
> reports `invalid cast to void*_Owned of uninit value: s.a.a.…a.p`. N=10 clean;
> plain-C N=11 twin valgrind-clean (11 alloc/11 free) → REJECT is wrong.
> sample_find: `/tmp/oracle_trial.boundary_owned_depth11.cbs`. No bug_log dup.

This validates the entire thesis: (1) the method-diversity track works; (2) the
user's PIVOT to subtle bugs was correct; (3) families 1-4 (re-borrow/field-nest/
lifetime/FieldPath) are correctly UNBOUNDED — only the _Owned-init recursion has
the depth cap. The soundness oracles were structurally blind to this (safe code,
no crash); only a boundary-sweep on an ACCEPTED program surfaced it.

**FIRST ROTATION (the bandit's whole purpose).** src-reading (deployed since
inception, recent yield 0 on the saturated soundness surface) → DEMOTED;
`accept-reject-boundary-bisection` (reward 5.5) → DEPLOYED. The campaign's active
method is now boundary-sweep over the false-positive/precision surface.
**Workable count: 1 of 3.** Next: trial r7 ranks 3-4 (corpus-shrink, ast-dump) +
the boundary oracle has more un-swept scalar dimensions to exploit.

### Round 7 — 2026-06-05 — bsc-oracle-scout (PIVOT: subtle bugs, not soundness)

User redirected after the 8-for-0 soundness saturation: "we need oracles for
SUBTLE bugs." Steered onto false-positive / precision / internal-inconsistency /
boundary observables — things that DON'T crash, so the 8 soundness oracles are
structurally blind to them. Scout's key discovery: the **maintainers' own
must-compile corpus** `clang/test/BSC/Positive/{InitAnalysis,NullabilityCheck,
SafeZone,Ownership/{Borrow,Owned,ArrayElem}}` is in-scope, ground-truth-ACCEPT,
and used by NO prior arm — a fresh input source with built-in ground truth.
4 brand-new arms, on bench:

1. **`accepted-twin-precision-metamorphic`** (rank 1): start from an ACCEPTED
   Positive/-corpus seed; apply a proven safety-preserving rewrite (identity
   _Borrow routing, no-op temp, duplicate-body branch, redundant grouping) that
   only makes value-flow SUBTLER. Verdict must STAY ACCEPT; a flip to REJECT = a
   precision/false-positive bug. Strict INVERSE of the demoted position-equiv arm
   (which started from REJECTED seeds seeking accept-flips). Guards: baseline
   accepts; flip is an in-scope diagnostic not a parse error; rewritten program
   valgrind-clean (proves it's safe → REJECT is wrong). ~80 compiles. Cheapest.
2. **`accept-reject-boundary-bisection`** (rank 2): parameterize an accepted
   skeleton by a scalar (field-nest depth, re-borrow count, lifetime span,
   FieldPath narrow depth) whose CORRECT boundary is "no limit"; bisect the flip
   point — any finite ACCEPT→REJECT = off-by-one/silent-cap. ~35 compiles.
3. **`corpus-shrink-equivalence-fp`** (rank 3): delete a provably-irrelevant stmt
   (use-scan whitelist) from an ACCEPTED program; verdict must stay invariant — a
   flip = acceptance was load-bearing on noise (right-verdict-wrong-reason). Plus
   monotone guard-ablation. ~46 compiles.
4. **`ast-dump-move-point-consistency`** (rank 4): read the BSCIR dump
   (existing dump-owned/borrow/combined-check test interface) as the observable;
   diff the move/borrow/narrow POINT across equivalent programs. Internal-state
   inconsistency. Higher setup (dump normalization) — self-flagged, hold.

**Next**: trial ranks 1 & 2 in parallel (cheap, independent observables, both
target the never-mined false-positive/boundary surface).

### Round 6 — 2026-06-05 — bsc-oracle-trial ×1 (score round-5 rank-1, the decisive bet)

`rewriter-runtime-equivalence`: **0 discrepancies / 30 programs.** The
source-to-source rewriter is RUNTIME-FAITHFUL for every in-scope construct
(stdout, exit, heap balance, valgrind all match .cbs vs rewritten-C). The known
rewriter bugs (F09/F54/F59) are compile-time artifacts, not runtime divergences.
A clean soundness result — NOT a fold.

**SATURATION VERDICT (8-for-0 on workable; the bandit has spoken).** Across 6
rounds (3 scout, 8 trials over 3 subsystems — analyzer, codegen/rewriter,
borrow), ZERO oracles scored a new-distinct bug. The track itself works perfectly:
it generated 11 diverse, well-grounded, sound oracles and scored them rigorously
and honestly. The bottleneck is NOT method diversity — it is that **the
bug-discovery surface reachable by differential/metamorphic/runtime oracles is
mined out after 91 filed bugs.** Discrepancies either fold into ~10 filed roots
(F75/F26 a strong attractor) or the subsystem is proven SOUND (inter-proc
ownership model; loop fixpoints; rewriter runtime-fidelity). This is itself a
valuable, defensible finding: the campaign is near-saturated for these methods.

**The bench is non-stationary regression value.** Every demoted arm is a sound
detector; once the fork ships fixes for F75/F26/F84-91/F22/F86, re-trialing them
will fire again (regression catch). Re-run the bench after each FIXED.md update.

Untrialed arms remaining (low expected new-bug yield, all self-flagged): r3-rank4
`compound-assign-nullable-field-roundtrip`, r5-rank2 `opt-level-output-invariance`,
r5-rank3 `borrow-accept-runtime-alias-witness`. Escalated to user for a
keep-going / redefine-workable / stop decision.

### Round 5 — 2026-06-05 — bsc-oracle-scout (steered: DIFFERENT subsystems, non-foldable)

After 7-for-0 on the analyzer surface, steered OFF analyzer-verdict differentials
onto subsystems whose observable can't fold into the filed analyzer bugs. Scout
did real grounding (read BSCIRBuilder/CGBuiltin/CGCall notes): BSC synthesizes NO
implicit owned-drop, CGCall has no `ENABLE_BSC` arm, `_Borrow`≠`noalias` — so a
naive destructor-count or noalias-aliasing differential finds nothing. Reshaped
candidates accordingly. 3 brand-new arms, on bench:

1. **`rewriter-runtime-equivalence`** (rank 1, the winner): build+RUN both `P.cbs`
   and the C from `clang -rewrite-bsc P.cbs`; compare the 4-tuple (stdout, exit,
   alloc==free ledger, valgrind-error-count) at -O0. Genuine = any field differs
   AND the `.cbs` baseline is valgrind-clean (UB-free) AND `P.c` compiled.
   Structurally NON-foldable: both programs are analyzer-accepted, so a runtime
   diff is purely the source-to-source rewrite changing meaning — orthogonal to
   every analyzer accept/reject bug. Strictly stronger than in-repertoire
   `rewrite-roundtrip` (which only checked compile-clean → F09). Inputs: ~25
   deterministic in-scope programs exercising reprint-risky constructs (non-void
   call operands of &&/||/comma that Prologue hoists to temps, ?: with _Borrow
   operands, eval-order chains). ~50 compiles + 50 runs.
2. **`opt-level-output-invariance-ubfree`** (rank 2): -O0 vs -O1/2/3 on programs
   whose -O0 run is valgrind-CLEAN (hard UB-free gate — this is what stops it
   folding into F86/F88/F90, whose baselines are dirty). Divergence = LLVM
   miscompile of a BSC lowering (SafeExpr emitters, agg _Owned copy, move/take
   builtins). Self-flagged low-yield (BSC lowerings are thin). ~100 compiles.
3. **`borrow-accept-runtime-alias-witness`** (rank 3): write 0xBEEF through one
   accepted _Borrow, read through the other; sentinel laundering = borrow FN.
   Union cases EXCLUDED (F42 attractor) + F39 cross-check. Self-flagged highest
   fold risk. ~20 compiles.

**Next**: trial rank-1 alone first — it's the decisive, cheap, structurally-
non-foldable bet. Its result determines whether ANY non-folding oracle can score
workable, or whether the campaign is genuinely saturated (→ escalate to user).

### Round 4 — 2026-06-05 — bsc-oracle-trial ×3 (score round-3 arms 1-3)

| arm | n | discrep | genuine | dup | folds to / finding |
|-----|---|---------|---------|-----|--------------------|
| merge-asymmetry-branch-join-generator | 30 | 4 | 0 | 4 | F26 (mergeDPVD DerefPath absent-key) + F75 (OwnershipImpl UNION); mergeVD/mergeFP guarded by initStatus pre-pop — no new hole |
| loop-fixpoint-iteration-divergence | 24 | 1 | 0 | 1 | F75 again (loop back-edge is the SAME UNION-not-MEET join); 21/24 sound — scalar/nullability/init fixpoints are correct |
| interprocedural-owned-handoff-contract-diff | 30 | 0 | 0 | 0 | NO discrepancy — call-convention model (by-value=consume, return=produce) proven correct across all standard shapes |

**Saturation finding (7-for-0 on workable, across rounds 2+4).** Every oracle is a
sound, low-false-alarm DETECTOR, but the new-bug surface they reach is mined out:
all discrepancies funnel into ~10 already-filed roots, and **F75/F26 (merge
UNION-not-MEET) is a strong ATTRACTOR** — if-join, switch-case, loop back-edge,
and struct-field inputs ALL collapse onto it. Two surfaces were positively proven
SOUND (inter-proc ownership model; scalar/nullability/init loop fixpoints) — a
useful negative, but 0 new bugs. **Conclusion: verdict-differential oracles over
the analyzer surface are saturated.** The remaining shot at non-folding oracles is
DIFFERENT SUBSYSTEMS — codegen optimization-level differential (destructor
drop/dup at -O2), borrow-checker region inference (heavyweight, under-read), and
rewriter runtime-equivalence (not just compile-clean). Round 5 steers there.
If round 5 also folds, the honest bandit verdict is "campaign saturated; bench
arms are REGRESSION oracles, not new-bug oracles" — escalate to the user.

### Round 3 — 2026-06-05 — bsc-oracle-scout (steered: novel-input, not repro-seeded)

Steered onto the round-2 lesson: pair a working detector with an input generator
aimed at UNREAD surface, not known-buggy seeds. During orientation the scout
located a concrete unaudited hole: the 3 nullability merge fns
(`mergeVD/mergeFP/mergeDPVD`, BSCNullabilityCheck.cpp:844-892) keep a key's
narrowed value when present in only ONE predecessor (else-branch raw insert) — an
asymmetric-key-absence merge hole, distinct from F75 (ownership UNION) and F26
(mergeDPVD untracked-as-Nonnull). 4 brand-new arms proposed (now on bench):

1. **`merge-asymmetry-branch-join-generator`** (rank 1): generate one-sided-fact-
   at-CFG-join programs (if-no-else / switch one-case / loop back-edge) with
   BOTH-sided and NEITHER-sided twins from the same template. Genuine = one-sided
   ACCEPTS while neither-sided REJECTS (proves the fact-absent value is rejected
   in isolation, so acceptance is a merge artifact). Confirm valgrind down the
   fact-absent edge. Targets mergeVD/FP/DPVD + OwnershipImpl::merge +
   InitAnalysis::merge. ~30 compiles + 6 vg.
2. **`loop-fixpoint-iteration-divergence`** (rank 2): minimal loop whose back-edge
   carries a degraded (moved/null/uninit) fact, vs a byte-faithful 2-iteration
   UNROLLED straight-line twin (the spec). Genuine = loop ACCEPTS, unrolled twin
   REJECTS, valgrind faults on iter-2. ~24 compiles + 6 vg.
3. **`interprocedural-owned-handoff-contract-diff`** (rank 3): 2-function owned
   chain vs a hand-INLINED single-function twin (restricted to single-call,
   non-recursive, by-value-param, no-static, single-return helpers → inline is
   provably semantics-preserving). Genuine = call-form and inline-form disagree on
   ACCEPT/REJECT. Reaches the Mode-2 handoff class src-reading can't. ~20 compiles
   + 5 vg.
4. **`compound-assign-nullable-field-roundtrip`** (rank 4): identity-valued
   compound-assign on a nullable FIELD (FieldPath / deeper `s->g->f`) vs the no-op
   twin; genuine = op-form ACCEPTS, no-op twin REJECTS. Field-keyed variant of
   F48/F50 (which were var-keyed). Self-flagged highest fold risk. ~16 compiles.

**Next**: trial ranks 1-3 in parallel (strongest unfiled-surface arguments);
hold rank 4 unless the first three don't reach 3 workable.

### Round 2 — 2026-06-05 — bsc-oracle-trial ×4 (score the round-1 bench)

All four round-1 arms trialed in worktree sandboxes (report-only). **Unanimous
result: every arm `works:false`, `genuine_distinct:0` — and every arm reliably
surfaced REAL, runtime-confirmed bugs that all fold to already-FILED ones.**

| arm | n | discrep | genuine | dup_existing | false_alarm | folds to |
|-----|---|---------|---------|--------------|-------------|----------|
| position-equivalence-metamorphic | 30 | 10 | 0 | 7 | 3 | F90 (all 7 condition-positions = one SwitchInt root) |
| narrowing-invalidation-fuzz | 24 | 8 | 0 | 8 | 5 | F84/F85/F87/F89 (all mutation forms within their fix scope) |
| flag-monotonicity-differential | 100 | 0 | 0 | 0 | 0 | — (monotonicity HOLDS; F86 reproduces as A=ACCEPT/B=ACCEPT, not a flip) |
| double-free-conservation-runtime | 20 | 3 | 0 | 3 | 0 | F75 / F91 / F22 |

**Cross-cutting finding (the actionable one):** the *detector* half of oracle
design is SOLVED — position-invariance, narrowing-invalidation, and
conservation-law are all sound and low-false-alarm. What gates `genuine_distinct`
is the **input source**: all four arms were seeded from existing repros / the
`cases/` corpus, so they re-derive already-filed bugs by construction. Three of
the four trial agents independently recommended the SAME fix: **re-seed with
genuinely unprobed input shapes** — multi-function / inter-procedural ownership
chains, loop-join narrowing, compound-assign on nullable fields, deeper nesting,
control-flow shapes NOT in `bug_log.md`. The next scout round is steered onto
this: pair a working detector with a NOVEL input generator aimed at UNREAD
analyzer surface (low-coverage files per `INDEX.md`), not at known-buggy seeds.

**Rotation decision:** none — `src-reading` stays deployed; bench arms demoted
(regression value retained). The track's first lesson is a *meta* one: oracle
novelty ≠ detector novelty; it's detector × input-novelty. Encode it in round 3.

### Round 1 — 2026-06-05 — bsc-oracle-scout (bootstrap, smoke test)

**Orientation**: bench empty. Fresh bugs F84–F91 dominated by two mechanical
patterns currently found by hand-prediction: (a) accept/reject ASYMMETRY where
the same value is read through a different syntactic position (F88 call-vs-read,
F90 switch-discriminant-vs-read, F91 comma-cast-vs-direct), (b) STALE narrowing
after an un-invalidated mutation (F84/85/87/89). High-value oracles mechanize the
comparison so the pair generates the hypothesis.

4 candidates proposed (all brand-new, all bench'd above). Trials NOT yet run —
this round was a propose-only smoke test of the scout step. Recipes:

1. **`position-equivalence-metamorphic`** (rank 1): seed = 12–15 repro/ programs
   REJECTED for using value V (uninit/moved/nullable). Per seed, emit ~7 variants
   moving the read of V into call-callee / switch-if-while discriminant / comma-cast
   / subscript-base / return / init-RHS positions, V's type+context fixed. Verdict
   = compile ACCEPT (exit 0, no diag naming V) vs REJECT. Flip ACCEPT-where-seed-
   REJECT = bug. Confirm flips with valgrind (uninit-read/null-deref/leak). Guard:
   seed must be rejected on V; exclude unevaluated positions (sizeof) and
   type-changing casts; a *different* diagnostic ≠ a flip. ~30 compiles + ~5 vg.
2. **`narrowing-invalidation-fuzz`** (rank 2): 3–4 seeds with a correctly-ACCEPTED
   narrowed deref (`if(p){*p}`, field base, deep FieldPath). Splice one mutation
   between narrow and deref (assign / `p=nullptr` TWIN / clear(&_Mut p) / alias-null
   / `+=0` / swap / field-write / nested field null), ~8 variants × 3 seeds.
   Verdict under `-nullability-check=all`. Genuine flip = ACCEPT while the
   `p=nullptr` twin on the same seed = REJECT (twin pins baseline invalidation).
   Confirm with valgrind SIGSEGV at deref. ~24 compiles + ~6 vg.
3. **`flag-monotonicity-differential`** (rank 3): ~30 programs (15 pass-default,
   15 fail-default). Config A = default, Config B = default + `-nullability-check=
   all` (+ strictest init/owned). Same binary, same bytes. Anomaly = A REJECT & B
   ACCEPT (strict-superset config accepting more = subtracted check), plus the
   annotation-strength axis (raw/_Nullable/_Nonnull of identical uninit deref).
   Guard: B must be a literal superset of checks; same-reason different-wording ≠
   flip. ~60 compiles. Self-flagged: may mostly re-derive F86.
4. **`double-free-conservation-runtime`** (rank 4): ~20 analyzer-ACCEPTED _Owned
   programs with branches/loops. Link a counting shim over safe_malloc/safe_free
   (alloc count, free count, per-address freed-set); drive each with ≥2 inputs to
   hit both branches. FAIL = allocs≠frees OR address freed twice OR live owned ptr
   leaked at scope exit. Guard: program must be fully ACCEPTED; count only
   executed paths; exclude _Unsafe. Self-flagged: highest overlap with demoted
   `valgrind-on-safe`.

**Rotation decision**: none — bench unscored, `src-reading` stays deployed.
**Next**: trial rank-1 (`position-equivalence-metamorphic`) — cheapest setup
(seeds exist), highest expected distinct-find rate, exercises UNPROBED terminator
positions (Goto/Drop discriminants adjacent to F90).

<!-- ROUND-LOG-END -->

## Oracle: rewrite-semantic-equivalence (added 2026-06-29)
DECISION: a program's RUNTIME behavior (valgrind alloc/free/error counts, exit, output) must be identical whether
BSC-compiled directly OR rewritten to C (`-rewrite-bsc`) then recompiled. A divergence = rewriter miscompile
(silent drop/double-drop/reorder). ORTHOGONAL to the F116 rewrite-PORTABILITY oracle (which only checks gcc-COMPILABILITY).
TRIAL: `$CL $INC -o a prog.cbs $SAFE; $CL $INC -rewrite-bsc prog.cbs -o prog.c; $CL $INC -o b prog.c $SAFE`; valgrind both, diff counts.
SCORE 2026-06-29: PROBED-SOUND on owned-loop/conditional/field/nested drops (all MATCH). Low yield on owned so far.
NEXT: borrow-flow, nullability-flow, control-flow-heavy (goto/switch), -O2 vs -O0 of the rewritten C.

## Oracle: rewrite-then-reanalyze-fixpoint (Oracle-Scout #1, trialed 2026-06-29)
DECISION: recompile the -rewrite-bsc output; a NEW diagnostic on rewrite(P) absent from P = rewriter changed
well-formedness (F09/F116 class). Distinct from rewrite-runtime-equivalence (runtime) and rewrite-roundtrip (compiles-or-not).
TRIAL 2026-06-29: 14 in-scope programs (6 owned: ternary/return-ternary/comma/borrow-deref/null-guard/&&||; 8 borrow:
reborrow/two-borrows/borrow-in-cond/nested-call/struct-field/owned-addr/chained/loop) → ALL MATCH (no new diagnostic on
rewritten output, proper new-diag set-diff). The F09 `_borrowck_tmp_N` malformation class is GONE on the current compiler.
SCORE: PROBED-SOUND on in-scope slice, high confidence, cheap (syntax-only). Bench-keep for regression; low current yield.
