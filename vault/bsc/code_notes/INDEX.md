# Code Notes — Index

Each note file summarizes one BSC analyzer source file: the **invariant** each function promises, its **peers**, and **candidate violations** ranked for probing.

**Read this first every session.** Use to decide which area to explore next and to avoid re-reading source you already understand.

## Session start checklist

0. `bash scripts/check_docs_sync.sh` — fails loudly if the index docs drifted
   from the SSOTs (bug_log.md, code_notes/*.md). Fix any drift before hunting.
1. Read this INDEX to see what's covered.
2. Read `_playbook.md` — note which defect classes are CONFIRMED (stop probing variants).
3. Read `_probed.md` — every probe shape that's been tried. Don't repeat.
4. Pick a function with **UNPROBED** candidates from a per-file note.
5. Apply invariant-driven reading discipline. Probe to confirm, not to discover.
6. **(Multi-agent only)** If asked to run as Conductor, read `docs/AGENTS.md` first —
   the spawn/handoff protocol + the ≤6-Explorers-in-parallel cap. Default mode is single-agent.

## How to use

- Open the note for the file you're touching. Read invariant + candidates.
- If a function isn't covered, read source and append a new entry.
- If a function has moved (line numbers off), re-read and refresh entry.
- New defect classes go in `_playbook.md` (vocab catalog, used to classify candidates).
- Every probe (regardless of outcome) appended to `_probed.md`.

## Per-file coverage

_"Latest"-edited dates are intentionally NOT tracked here (they drift) — use
`git log -1 --date=short -- code_notes/<file>` for the real last-touch date._

| Note file | Source | Status / filed |
|-----------|--------|----------------|
| `BSCOwnership.md` | `clang/lib/Analysis/BSC/BSCOwnership.cpp` | partial — checkSUse/checkSFieldAssign/checkSFieldUse F44/F45/F61/F67; HandleDREUse, checkMemoryLeak read |
| `BSCBorrowChecker.md` | `clang/lib/Analysis/BSC/BSCBorrowChecker.cpp` | partial — DefUse/ActionExtract visitors, RegionCheck/InferenceContext/DFS/Liveness/LoansInScope read; F11/F21/F24/F39/F42 |
| `BSCNullabilityCheck.md` | `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp` | partial — getExprPathNullability, VisitBinaryOperator/CallExpr/UnaryOperator; F18/F48/F50 |
| `BSCNullabilityCheck_narrowing.md` | `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp` (narrowing CFG) | path-fact narrowing across loop/switch/goto/short-circuit/reassign — FieldPath-not-invalidated-on-reassign F84; rest SOUND (Chain X) |
| `nullability_indirect_call.md` | `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp` (VisitCallExpr callee position) | `_Nullable` fnptr as CALLEE — bare `fp()`/`s.fp()` = FOLD-F31 (callee never visited); explicit `(*fp)()` deref-callee path COMPLETE + narrowing-applied (SOUND). SATURATED @28656aa9 (R4E5) |
| `composition_init_null.md` | `BSCIRInitAnalysis.cpp` × `BSCNullabilityCheck.cpp` (cross-analyzer) | init-analysis × nullability PAIR on `_Nonnull` slots after partial-init/merge/default-zero/ensure_init — uninit/merge/field/array/default-zero all SOUND; ensure_init×_Nonnull-pointee under audit |
| `composition_borrow_init.md` | `BSCBorrowChecker.cpp` × `BSCIRInitAnalysis.cpp` (cross-analyzer) | borrow-checker × init-analysis: read/write through a borrow of an UNINIT/MaybeInit/MOVED referent — SATURATED-SOUND @ 28656aa9. The borrow-CREATE site (`&_Mut x`/`&_Const x`) is the init firewall (= a use of the referent at whole-local/field/nested/MaybeInit granularity); move/write-while-borrowed + borrow-of-moved all caught. F83's Index-skip is dest-WRITE-only (the Ref form IS checked) |
| `BSCIRInitAnalysis.md` | `clang/lib/Analysis/BSC/BSCIRInitAnalysis.cpp` | thorough — transferStatement, markFieldInit/tryPromoteParent, getFieldPath; F15(LOW)/F52/F55 |
| `BSCIRBuilder.md` | `clang/lib/Analysis/BSC/BSCIRBuilder.cpp` | partial — lowerStmt dispatch; F52 (attributed-stmt drop) |
| `BSCNullCheckInfo.md` | `clang/lib/Analysis/BSC/BSCNullCheckInfo.cpp` | partial — extractDistinguishedTrackablePtr (SafeExpr-strip, F65), operator|=/&= |
| `SemaBSCOwnership.md` | `clang/lib/Sema/BSC/SemaBSCOwnership.cpp` | partial — CheckTemporaryVarMemoryLeak F14/F47, CheckMoveVarMemoryLeak F62, fnptr checks F41 |
| `SemaBSCSafeZone.md` | `clang/lib/Sema/BSC/SemaBSCSafeZone.cpp` | partial — IsSafeConversion F51, IsSafeFunctionPointerTypeCast (CONFIRMED-new), IsSafePointerConversion PROBED-SOUND (pointee-only; nullability backstopped by SemaDeclBSC gate), G08 compound-assign bypass |
| `SemaDeclBSC.md` | `clang/lib/Sema/BSC/SemaDeclBSC.cpp` | partial — Prologue/Epilogue F40, HasDiff*Qualifiers redecl F53/F56, CheckNullabilityQualTypeAssignment F66 |
| `SemaStmtBSC.md` | `clang/lib/Sema/BSC/SemaStmtBSC.cpp` | read — no bug surface (ActOnSafeStmt/Expr pure construction; CheckBSCConstexprCondition type gate); + BSC file-coverage inventory |
| `SemaTemplateInstantiateDeclBSC.md` | `clang/lib/Sema/BSC/SemaTemplateInstantiateDeclBSC.cpp` | generic monomorphization: `_Owned`/`_Borrow` AttributedType sugar SURVIVES `getCanonicalType` (only nullability sugar stripped, cf G12); owned- + borrow-through-monomorph both PROBED-SOUND (bi9znvfyo/by23vqkb8) |
| `BSCIRDataflow.md` | `clang/include/clang/Analysis/Analyses/BSC/BSCIRDataflow.h` | generic fwd/bwd dataflow framework (worklist) — entry-block back-edge skip PROBED-SOUND (dedicated pred-less entry block); exit-monotonicity + transferTerminator per-edge refinement UNPROBED |
| `TypeBSC.md` | `clang/lib/AST/BSC/TypeBSC.cpp` | partial — AreOwnedBorrowQualifiersCompatible F57, hasOwnedRetOrParams F41; plain generic-alias qualifier-survival PROBED-SOUND (bm57v78zi) |
| `WalkerBSC.md` | `clang/include/clang/AST/BSC/WalkerBSC.h` | partial — BSCFeatureFinder F59, SafeFeatureFinder::VisitQualType PROBED-latent-unreachable (missing VisitType recursion real but not exploitable; array-of-owned shape-rejected, violations always involve body-caught owned/borrow exprs) |
| `RewriteBSC.md` | `clang/lib/Frontend/Rewrite/RewriteBSC.cpp` | partial — F09 surface, RewriteNonGenericFuncAndVar F54 |
| `CGExprAgg.md` | `clang/lib/CodeGen/CGExprAgg.cpp` | AggExprEmitter missing VisitSafeExpr — F60 |
| `CGExprComplex.md` | `clang/lib/CodeGen/CGExprComplex.cpp` | ComplexExprEmitter missing VisitSafeExpr — F63 |
| `CGExprConstant.md` | `clang/lib/CodeGen/CGExprConstant.cpp` | ConstExprEmitter/ConstantLValueEmitter — ONLY emitter missing VisitSafeExpr, but SafeExpr-in-constant-position PROBED-SOUND (latent-unreachable: ExprConstant APValue path folds it first) 2026-06-29 |
| `CGBuiltin.md` | `clang/lib/CodeGen/CGBuiltin.cpp` | BI__assume_initialized drops side-effecting arg — F58; owned-drop emission PROBED-SOUND (no implicit drops) |
| `SemaChecking_ArrayElem.md` | `clang/lib/Sema/SemaChecking.cpp` (BSC `_ArrayElem` builtins) | __move/take_array_from_raw + _ArrayElem move/free/borrow tracking — PROBED-SOUND (2026-05-30) |
| `SemaChecking_RawTransfer.md` | `clang/lib/Sema/SemaChecking.cpp` (raw-transfer builtins) | handleBSCRawTransferBuiltin qualifier strip/re-add — PROBED-SOUND (const/volatile/nullability preserved, 2026-05-30) |
| `SemaBSCOverload.md` | `clang/lib/Sema/BSC/SemaBSCOverload.cpp` | mixed-mode `_Safe`/`_Unsafe` overload selection (Chain L) — PROBED-SOUND (nested fold = F76, 2026-05-30) |
| `SemaExpr_BSC.md` | `clang/lib/Sema/SemaExpr.cpp` + `SemaExprMember.cpp` (BSC `&_Mut`/`&_Const` build path, err_safe_mut global gate, member-result qualifier combine) | `&_Mut "lit"[i]` re-confirmed = F36; err_safe_mut + member-combine + `&_Mut` const all PROBED-SOUND @28656aa9 (2026-05-30, R2E6) |
| `ParseDeclBSC.md` | `clang/lib/Parse/BSC/ParseDeclBSC.cpp` (BSC generic-decl parser) | block-granularity read (BACKLOG #8) 2026-06-18 — ParseBSCTemplateParameters angle-close missing `>=` arm = CONFIRMED-G11 (no-space `typedef A<T>=...` FP); homes the fuzz-found generic CRASHES G02/G03/G04/G05 (parser sites) |
| `ParseStmtBSC.md` | `clang/lib/Parse/BSC/ParseStmtBSC.cpp` | ParseSafeStatement (_Safe/_Unsafe stmt modifier + scope save/restore) + CheckStmtTokInSafeZone (parse-gate=asm only; rest at Sema) — PROBED-no-bug-surface; file-level coverage complete |
| `DeclSpecBSC.md` | `clang/lib/Sema/BSC/DeclSpecBSC.cpp` | declspec setters: `setFunctionSafeZoneSpecifier` (≤1 safe-zone spec/fn, dup+conflict both fail-closed) PROBED-SOUND 2026-06-25; `setFunctionSpecAsync` = async/coroutine OOS |
| `MangleBSC.md` | `clang/lib/AST/BSC/MangleBSC.cpp` (generic name mangling) | Explorer read 2026-06-18 — getBSCTypeName mangles via printed type name + char-substitution; UNPROBED candidate: distinct QualTypes that print alike → symbol collision → wrong-answer monomorphization |
| `ExprBSC.md` | `clang/lib/AST/BSC/ExprBSC.cpp` (BSC Expr predicates) | read 2026-06-23 — `Expr::isNullExpr` dyn_cast ladder missing `DeclRefExpr`/`ConditionalOperator` arms → null-owned-var copy mis-classified non-null → ownership `setToOwned` → `checkMemoryLeak` FP leak; F108 (peer `getExprPathNullability` DOES classify same expr Nullable → C2+C7) |
| `DeclBSC.md` | `clang/lib/AST/BSC/DeclBSC.cpp` (in-scope subset) | read 2026-06-23 — only `classifyEnsureInit` in scope (rest = traits/methods, OOS); 2-path Decl-attr × ExtParameterInfo classify, 4 callers (SemaExpr + 3×InitAnalysis); candidates: NumParams divergence, Decl-mask-ExtInfo, nested-struct partial-init ensure_init FN (probe below) |

**Snapshot suite**: every `repro/F*.cbs` has a committed `.expected.{yaml,stderr}`
baseline. Run `scripts/check_probes.sh --repros-only` to detect any
behaviour change after a compiler rebuild. See `scripts/test_pr.sh` for
the PR-testing workflow and `scripts/check_chain_reopen.sh` for which
saturated chains a commit range reopens.

## Workflow (invariant-driven reading)

For each function:

1. Write the **invariant** in one sentence (precondition / postcondition / what observable is protected).
2. Note **peers**: functions that must agree on the same key / kind / state.
3. List **candidate violations** under 3 lenses:
   - reachability (state code didn't anticipate)
   - symmetry (peer disagrees)
   - composition (atomic-safe but wrapper-unsafe)
4. Rank top candidate, write a probe to confirm.
