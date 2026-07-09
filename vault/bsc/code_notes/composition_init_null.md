# composition_init_null.md — init-analysis × nullability cross-analyzer surface

Hunting the PAIR gap: a `_Nonnull` pointer slot must be (a) INITIALIZED (BSCIRInitAnalysis
use-of-uninit) AND (b) treated as non-null (BSCNullabilityCheck `getDefNullability`→NonNull,
so a deref is accepted with no diag). After partial-init / move / CFG-merge / default-zero /
ensure_init-contract, do the two analyzers compose soundly, or does each assume the other
covers the case → an uninit-or-null `_Nonnull` deref accepted by BOTH?

Peers:
- BSCIRInitAnalysis `checkOperand` (:829) — use-of-uninit diag (the ONLY init guard).
- BSCNullabilityCheck `getDefNullability` (:238) — a `_Nonnull`/`_Owned`/`_Borrow` pointer's
  declared default is **NonNull**; a deref of such a pointer is accepted with no null-check.
- BSCNullabilityCheck `CheckInit`/`FindNonnull` (:478/:256) — the decl-time default-init
  nonnull rejection (F78 lives here).

## State table (28656aa9, `-nullability-check=all`)

| shape | init verdict | nullability verdict | composite | sound? |
|-------|--------------|---------------------|-----------|--------|
| `int *_Borrow _Nonnull p; sink(p)` (uninit, direct use) | REJECT use-of-uninit | (would assume NonNull) | REJECT | yes — init guard fires |
| `int *_Borrow _Nonnull p; if(c)p=q; sink(p)` (merge maybe-uninit) | REJECT possibly-uninit | — | REJECT | yes |
| `struct S{_Nonnull a;int b}; s.b=5; sink(s.a)` (field a uninit) | REJECT uninit `s.a` | — | REJECT | yes |
| `struct S s = {0}` / `{}` / `{.b=5}` (a default-zero) | (init OK — zero IS init) | REJECT `nonnull cannot be assigned by nullable` / `must be properly initialized` | REJECT | yes — nullability CheckInit fires (F78 fix in NumInits==0/partial branch) |
| `int *_Borrow _Nonnull arr[3]; arr[0]=q; sink(arr[1])` | REJECT uninit `arr` (array conservatism) | — | REJECT | yes |
| `arr[3]={0}` of `_Nonnull` | — | REJECT must-be-init | REJECT | yes |
| `struct S{_Nonnull arr[2]}; s.arr[0]=q; sink(s.arr[1])` | REJECT uninit `s.arr` | — | REJECT | yes |
| narrowed `if(q!=0){s.a=q}` then sink(s.a) | OK | OK (q NonNull post-narrow) | ACCEPT | yes — genuinely nonnull |
| `struct S{_Nonnull a}; if(c){s.a=q}; sink(s.a)` (field merge) | REJECT possibly-uninit `s.a` | — | REJECT | yes |

## Findings so far

The init guard (use-of-uninit, including possibly-uninit at merge and field-granular `s.a`)
is the load-bearing analyzer for the UNINIT direction and is robust. The nullability CheckInit
decl-time default-init rejection (post-F78) is the load-bearing analyzer for the DEFAULT-ZERO
direction and is robust. The compositions tested above are JOINTLY SOUND.

## CONFIRMED-new (2026-05-30, R2E4 explorer) — uninit `_Nonnull` deref accepted by BOTH outside `_Safe`

**The gap**: in a NON-`_Safe` function, dereferencing an UNINITIALIZED `_Nonnull` pointer is
ACCEPTED with no diagnostic, and at runtime reads through a garbage/null pointer → SIGSEGV.

**Two analyzers' assumptions (each self-consistent, the PAIR has the hole):**
- `BSCNullabilityCheck` `VisitUnaryOperator` (BSCNullabilityCheck.cpp:673-682) emits the
  `NullablePointerDereference` diag ONLY when `getExprPathNullability(subExpr)==Nullable`. For a
  `_Nonnull` pointer, `getDefNullability` (:238-253) returns **NonNull**, so the deref check is
  SUPPRESSED — the checker TRUSTS the `_Nonnull` annotation and never verifies the slot was
  assigned a non-null value. This pass RUNS in non-`_Safe` code (it rejects the `_Nullable`/raw
  twin there — see asymmetry).
- `BSCIRInitAnalysis` `checkOperand` (BSCIRInitAnalysis.cpp:829) — the use-of-uninit guard that
  WOULD reject the uninitialized deref — runs ONLY inside `_Safe` zones (a plain uninit `int x;
  return x;` is rejected in `_Safe`, accepted in non-`_Safe`).
- So in a non-`_Safe` function neither pass covers the uninitialized `_Nonnull` slot: nullability
  defers (assumes the type is honest / init done elsewhere), init analysis is absent.

**Asymmetry (one-word baseline)**: `int *_Nonnull p; return *p;` → ACCEPTED; the byte-identical
`int *_Nullable p; return *p;` → REJECTED "nullable pointer cannot be dereferenced". The initialized
control `int *_Nonnull p = &v; return *p;` → clean. So it is the UNINIT, not the deref, that slips,
and the `_Nonnull` annotation is what flips REJECT→ACCEPT.

**Symptom**: false-negative; runtime Invalid read / SIGSEGV (vg: "Invalid read of size 4",
"Process terminating with signal 11 (SIGSEGV)", "Access not within mapped region at address 0x0").
Repro `/tmp/explorer_repro.*.cbs`, baseline `/tmp/explorer_baseline.*.cbs`. Needs
`-Xclang -nullability-check=all` (same opt-in mode as F18/F48/F50/F70/F78).

**DISTINCT from all filed nullability FNs**: F18/F48/F50 (arithmetic/compound-assign launders a
known-`_Nullable` origin to NonNull), F26 (merge), F29/F31 (fnptr/callee variance), F66 (`int**`
assignment short-circuit), F70 (De-Morgan invert) — every one of those has a value whose origin IS
`_Nullable` and gets re-typed NonNull by an OPERATION. Here there is NO operation and NO nullable
origin: the `_Nonnull` slot is simply NEVER INITIALIZED. The bug is the composition: nullability's
unconditional trust of the `_Nonnull` type + init-analysis's `_Safe`-only scope. DISTINCT from F78
(that was the FP over-reject direction of decl-time default-init; this is the FN under-reject of
an uninitialized RUNTIME deref).

**Blast radius**: partial-init struct `_Nonnull` field (`s->b=5; return *s->a;`), `_Nonnull` array
element, any uninit `_Nonnull` slot reached at a deref site in non-`_Safe` code. (Inside `_Safe`,
init analysis backstops the uninit case and `_Borrow _Nonnull` uninit IS rejected — the gap is
specifically the non-`_Safe`-zone-where-nullability-runs-but-init-doesn't cell.)

**Fix surface**: nullability's `_Nonnull`-deref suppression should be conditioned on the slot being
known-initialized (or `VisitUnaryOperator`/`VisitMemberExpr`/`VisitArraySubscriptExpr` should treat
an uninit-or-default `_Nonnull` slot as un-trusted). Equivalent: run the use-of-uninit check (or at
least a nonnull-was-assigned check) in non-`_Safe` zones for `_Nonnull` slots, since the deref-safety
is being claimed there.

## Open candidates (UNPROBED)

1. **ensure_init × _Nonnull pointee field** — **PROBED-SOUND 2026-06-29.** Within-TU ensure_init IS
   verified: an `ensure_init` callee whose body does not initialize `*p` is REJECTED ("'*p' not
   initialized at return in __attribute__((ensure_init)) function"); writing a `_Nonnull` field requires a
   nonnull value (else the nullability check fires). So a callee CANNOT satisfy ensure_init while leaving the
   `_Nonnull` pointee field null — no within-TU FN. The only residual is an EXTERN declaration lying about its
   contract = the documented by-design trust model (like any unsafe/extern contract), not a compiler FN. Also
   note: the raw `*p`/`&s` form is shape-rejected in `_Safe` (`*`/`&` forbidden), so the construct needs `_Borrow`.
   ~~RANK HIGH~~ → SOUND. [superseded original below]
   1b. **ensure_init param tells init analysis "the
   callee initializes `*p`"; init analysis credits the pointee Init after the call WITHOUT
   re-checking field-level null-ness. If a callee satisfies the ensure_init contract by writing
   only SOME fields (or zero-filling), can a `_Nonnull` pointee field be left null while init
   analysis trusts the contract and nullability assumes NonNull? RANK: HIGH (init defers to
   contract; nullability never re-checks a post-call pointee).
   **PROBED 2026-06-23 (PROBED-folded-into-F104):** `_Safe` def with `ensure_init` writing only
   `.x` (not `.p`) → REJECT (checkEnsureInitAtReturn fires). Hetero `_Unsafe` def FIRST + `_Safe`
   decl AFTER adds `ensure_init` + `_Safe` caller reads uninit `.p` → ACCEPTED clean + valgrind
   uninit-read (ERROR SUMMARY 1). 3-way asymmetry proven (`_Safe`-def rejects; hetero-writes-`.p`
   clean; hetero-no-contract rejects). This is the **unconditional-`ensure_init` variant of F104**
   — F104's blast-radius note explicitly covers it ("affects BOTH init contracts — `ensure_init`
   (unconditional) is laundered the same way ... one fix covers both"). FOLD-F104, not new.
   (`.fresh_verdict.tsv` now correctly marks `F104 open` with a detailed note; findings.jsonl =
   `do-not-file` since the user is fixing F104 directly. Re-validated 2026-06-29: repro still
   launders rc=0, consistent with open. The earlier "F104 fixed mis-mark" worry is resolved.)
2. **default-zero of a NESTED `_Nonnull`** — PROBED-SOUND 2026-06-26 @411b4118: nested-struct + array-of-struct
   `{}` both REJECT "must be properly initialized" (recursion reaches the leaf); union shape-rejected (_Safe forbids
   union access). F78/FindNonnull recursion robust. NOT a FN.
3. **move-out of `_Owned _Nonnull` then re-slot** — covered by E1 (FOLDED, nullability not
   load-bearing on move). Skip.

## ensure_init contract enforcement (caller-trust × callee-obligation × type-match) — PROBED-SOUND 2026-06-04

**Invariant**: a caller may treat an arg passed to an `__attribute__((ensure_init))` param as Initialized
after the call ONLY IF the callee is genuinely obligated to init `*param` and that obligation is enforced.

**Three legs (all probed SOUND)**:
1. Callee-side: `checkEnsureInitAtReturn` (BSCIRInitAnalysis.cpp:1140-1155) errors EnsureInitNotInit /
   EnsureInitMaybeNotInit at every return where the param's EnsureInitDerefState is Uninit/MaybeInit.
   Probe: decl+def both ensure_init, def `(void)p;` → "'*p' not initialized at return". SOUND.
2. Caller-side trust: terminator handler (:268-318) marks the arg place Initialized using ensure_init from
   the direct FunctionDecl param attr OR the CalleeProtoType ExtParameterInfo (indirect/fnptr). Delegation
   (:295-300) only for a plain `Copy(_N)` of an ensure_init param, no projections.
3. Type match: redecl mismatch (decl ensure_init, def not) → REJECTED "conflicting types for 'f'"
   (SHAPE-REJECTED). Outer fnptr-assignment mismatch → REJECTED (CheckEnsureInitFunctionPointerType,
   SemaBSCOwnership.cpp:879-918, F73 note). So the caller's trust is backed by a matching obligation.

**Peers**: F73 (CONFIRMED — NESTED fnptr param ensure_init mismatch NOT recursed → the one real hole),
F76 (fnptr param variance), entryState (:80 seeds EnsureInitDerefStates from the DEFINITION's param attr).

**Candidates**:
1. callee-return + redecl mismatch — **PROBED-SOUND/SHAPE-REJECTED**.
2. C-STYLE CAST of an ensure_init fnptr (vs assignment which F73/CheckEnsureInitFunctionPointerType covers)
   — does the cast path run the same ensure_init compat check? UNPROBED; likely folds into F73/F76 fnptr family.
3. delegation via a NON-plain-Copy pass (`f((p))`, `f(p+0)`) — would MISS delegation → false-positive
   (over-strict), not FN. UNPROBED, low priority.

## ensure_init callee partial-init SYMMETRY vs caller whole-credit (NESTED/aggregate pointee) — UNPROBED 2026-06-23

**Invariant**: the callee-side `checkEnsureInitAtReturn` (BSCIRInitAnalysis.cpp:1511) must
REJECT every return path where the ensure_init param's pointee is not FULLY initialized at
EVERY (nested) field; symmetrically the caller's plain-ensure_init credit
(transferTerminator :573-577) calls `markAllFieldsInit` over the WHOLE pointee tree, so
the caller TRUSTS full init. If the callee can satisfy/escape the at-return check with a
PARTIAL init, the caller over-credits → reads uninit nested field (FN).

**Mechanism the callee uses to promote the param's whole-pointee state**:
- `(*out).f = v` field write → markFieldInit (:235-237) → tryPromoteParent (:929).
- tryPromoteParent promotes `EnsureInitDerefStates[param]` to Init ONLY when ALL siblings
  at the top level are `Initialized` (:970-989, getNumFields(pointeeTy) siblings).
- ARRAY-typed fields are NEVER marked by a field/element write (:236 guard
  `!getFieldType(...)->isArrayType()`); they require `__assume_initialized` or `{}`. So an
  array field in the pointee should BLOCK promotion (→ FP, the F106-empty-struct cousin),
  NOT cause an FN. Confirm.
- `markAllFieldsInit` (caller, :1033) recurses into nested struct/union fields
  UNCONDITIONALLY (no init-state check) — it's the "trust the contract" credit.

**Peers**: getNumFields (:834, F106 root — empty struct counts as 1 field with 0 subfields),
tryPromoteParent (:929), markAllFieldsInit (:1033), F106 (empty-field blocks promotion =
the FP direction), F73/F100/F104 (contract laundering across fnptr/redecl).

**Candidates (ranked)**:
1. **nested-struct partial** — PROBED-SOUND 2026-06-26 @411b4118: write `in.x`, leave `_Nonnull in.p` → rc=1
   "'*out' not initialized at return"; ALSO sound at 2-level (`m.d.a`+`m.b` written, `m.d.q` left → fires).
   tryPromoteParent recursion is symmetry-correct (param stays MaybeInit until ALL nested fields init). The
   only laundering of this contract is the hetero-redecl path = F104 (filed). NOT a new FN.
2. **pointer-field-in-pointee**: pointee struct has a `_Nullable`/raw pointer field; callee
   writes the scalar but leaves the pointer field; does getNumFields count it → block?
   RANK: MED.
3. **array-field-in-pointee + __assume_initialized(&(*out).arr)** — does the assume mark the
   whole-array field, promoting the param even though elements are garbage? (assume = trust,
   so maybe by-design.) RANK: MED.
4. **conditional path**: one branch fully inits *out, other writes nothing; merge →
   MaybeInit → at-return MaybeNotInit fires (notes say SOUND). RANK: LOW (covered).

## _Owned _Nullable null-checked conditional consume — probe 2026-06-24
**Invariant**: `int *_Owned _Nullable p = mk(); if(p!=nullptr) consume(p);` — on the NULL branch p is null
(no free needed, null-owned doesn't leak); on the non-null branch p consumed. No leak FP on null path, no
double-free, consume tracked.
**Peers**: F108 (null-owned-var copy false leak), F43 (null-init owned field cast), setToNull, checkMemoryLeak.
**Candidates**: 1. null-branch leak FP — PROBED-SOUND 2026-06-26: `if(o!=0)consume(o)` rc=0 clean, null branch needs no free.
2. consume on BOTH branches (one null) double-free. 3. p reassigned after null-check.
