# DeclBSC.cpp — BSC Declaration AST (in-scope subset)

Source: `clang/lib/AST/BSC/DeclBSC.cpp` (269 lines). UNMAPPED before 2026-06-23.
The bulk of this file is Trait/ImplTrait/BSCMethod/TraitTemplateSpecialization machinery
(OUT OF SCOPE — traits/generics/_Owned struct member methods). The ONE in-scope function
is `classifyEnsureInit`.

## classifyEnsureInit (DeclBSC.cpp:23-45) — UNPROBED (read; enforcement callers probed separately)
**Invariant**: for function param index `I`, classify the ensure_init contract as
`None` | `EnsureInit` | `EnsureInitIfRet` (the last carries a `CondOut` return-value
that gates the contract). Two PARALLEL paths that must AGREE:
  - **Decl path** (:26-30, :38-40): `Decl->getParamDecl(I)->getAttr<EnsureInitIfRetAttr>()`
    / `hasAttr<EnsureInitAttr>()` — for direct calls where the FunctionDecl is known.
  - **ExtParameterInfo path** (:31-37, :41-43): `FPT->getExtParameterInfo(I).isEnsureInitIfRet()`
    / `isEnsureInit()` — for the FUNCTION-TYPE-sugar form (typedefs, function pointers,
    indirect calls where only the type is available).
Decl-attr checked FIRST; if it hits, ExtInfo is never consulted.
**Peers (the 4 callers, all must classify identically — Mode-2 handoff surface)**:
- `SemaExpr.cpp:7787` (Sema call-site arg check — emits `warn_ensure_init_not_addressof`;
  only checks the arg's FORM is `&x`/`&_Mut x` or a delegation; does NOT verify init).
  Iterates `NumCallArgs` (actual args).
- `BSCIRInitAnalysis.cpp:499` (terminator handler — records `PendingCondInit` for
  `EnsureInitIfRet`, credited on the matching SwitchInt/return edge).
- `BSCIRInitAnalysis.cpp:1389` (ExemptArgBases — exempts ensure_init arg bases from
  uninit-use FPs). Iterates `NumParams` capped by `CD.Args.size()`.
- `BSCIRInitAnalysis.cpp:1504` (ExemptArgIndices — for at-return check).
**Candidates** (ranked — ALL RESOLVED 2026-07-06, see _probed.md):
1. → SHAPE-REJECTED at source: classifyEnsureInit bounds-guards all 4 paths with `I < getNumParams()`
   (DeclBSC.cpp:26/31/38/41) so vararg/extra indices classify None; callee-side contract confirmed
   enforced on variadic fns (probes/ensure_init_variadic_extra_args.cbs). 2. → FOLDS-F104 (gate ignores
   ensure_init; direct-decl spelling probed 2026-06-29). 3. → PROBED-SOUND (2026-07-01 entry below).
   (original text follows)
   (C6 localized check skipped / peer NumParams divergence) Sema iterates `NumCallArgs`
   (actual args at the call site) while init-analysis iterates `NumParams` (declared).
   If a callee is called with FEWER args than declared (default-arg param, or a
   ensure_init param beyond the actual call args — e.g. a varargs tail or a defaulted
   last param), Sema checks the WRONG index set / skips the ensure_init param, while
   init-analysis exempts by declared index. Probe: ensure_init on a param whose index
   >= actual call-arg count (needs a default-arg form). RANK MED (default-arg reachability).
2. (C1 asymmetry Decl-attr masks ExtInfo) For a direct call where the `ensure_init` is
   on the function TYPE (typedef attr) but NOT redeclared on the Definition's param
   Decl, the Decl-path returns None at the definition site (callee-side
   `checkEnsureInitAtReturn` reads the DEF's param attr per composition note
   entryState :80) → the callee's at-return obligation is NOT enforced for a type-attr
   contract → caller trusts (FN). BUT composition note leg-3 says redecl mismatch is
   SHAPE-REJECTED ("conflicting types"); needs a form where decl+def types AGREE but
   only the TYPE carries the attr (no per-Decl attr). RANK MED.
3. (composition candidate #1, cross-file) nested-struct PARTIAL init in an ensure_init
   callee: `tryPromoteParent` (:929) over-promotes → caller whole-credit `markAllFieldsInit`
   (:1033) trusts full init → reads uninit nested field. THIS is the highest-signal
   unprobed candidate (see composition_init_null.md candidate #1, RANK HIGH). Probe below.

## 2026-07-01 ensure_init callee-side full-init verification — FIELD-GRANULAR, PROBED-SOUND
- `checkEnsureInitAtReturn` (BSCIRInitAnalysis.cpp:1511) tracks EnsureInitDerefStates per param; despite being one InitState per param, the underlying init lattice is FIELD-GRANULAR (sub-place keyed).
- Probes (`__attribute__((ensure_init))` on `struct S *_Borrow out`): partial-init `out->a` only (b uninit) → rc=1 "'*out' not initialized at return"; NESTED partial `out->a`+`out->in.x` (in.y uninit) → rc=1 REJECTED; full-init → rc=0; whole-struct write `out->in = i` → rc=0 (inits all sub-fields); no-init → rc=1. Closes the prior UNPROBED "nested-struct partial-init ensure_init FN" candidate — SOUND, no FN. Field-granular through arbitrary nesting.
