# SemaDeclBSC.cpp

Source: `clang/lib/Sema/BSC/SemaDeclBSC.cpp`.

Houses the borrow-checker's two AST transforms wrapping the analysis pass:

- **`BorrowCheckerPrologue`** (:434+) — normalizes the function body's AST so the CFG-based analysis sees a simpler shape. Rewrites side-effect-bearing sub-expressions into temporaries hoisted into outer `Stmts`, registers each replacement in `replacedNodesMap`.
- **`BorrowCheckerEpilogue`** (:944+) — restores the source-level AST by looking up `replacedNodesMap`. **Does not** redo Sema (`AlwaysRebuild()` returns false) to preserve destructor insertion semantics.

Both inherit from `TreeTransform<Derived>`.

## Invariant pair (C4)

For every AST kind X that one of them overrides, the other should have a peer that knows how to restore / receive the rewrite. Mismatches = C4 candidates.

### Coverage diff (as of 2026-05-19)

**In Prologue only:**
- `TransformConditionalOperator` (:818) — splits `?:` into IfStmt + VarDecl + DRE-to-temp
- `TransformPredefinedExpr` (:905) — string-literal-like
- `TransformStringLiteral` (:937) — string-literal-like
- `TransformStringLiteralLike` (:478) — helper

**In Epilogue only:**
- (none unique that I've seen)

**Identical in both:**
TransformStmt, CaseStmt, CompoundStmt, DeclStmt, DefaultStmt, DoStmt, ForStmt, IfStmt, LabelStmt, ReturnStmt, SafeStmt, SwitchStmt, WhileStmt, ArraySubscriptExpr, AwaitExpr, BinaryOperator, CallExpr, CompoundLiteralExpr, CStyleCastExpr, DeclRefExpr, ImplicitCastExpr, InitListExpr, MemberExpr, ParenExpr, SafeExpr, StmtExpr, UnaryOperator.

**Missing from BOTH:**
- `TransformChooseExpr` (`__builtin_choose_expr`)
- `TransformGenericSelectionExpr` (`_Generic`)
- `TransformBinaryConditionalOperator` (GNU `a ?: b`)
- `TransformAtomicExpr`
- `TransformVAArgExpr`
- `TransformOpaqueValueExpr`
- `TransformPseudoObjectExpr`

These fall through to `TreeTransform`'s default, which doesn't normalize sub-expressions or register replacements.

## Functions

### `BorrowCheckerPrologue::TransformConditionalOperator` — :818-856
**Invariant**: rewrites `cond ? T : F` (when borrow-relevant) into:
```
T_type tmp;
if (cond) tmp = T; else tmp = F;
<DRE to tmp>
```
The IfStmt is `push_back`-ed into the outer `Stmts` vector. The returned expression is the DRE. `replacedNodesMap.Insert(DRE, CO)` records the substitution for Epilogue restoration.
**Why this is load-bearing**: BSCBorrowChecker has no `VisitConditionalOperator`. Without the rewrite, both arms' source paths would collide in `Sources`. With the rewrite, both arms flow through the IfStmt's separate CFG basic blocks and join cleanly.
**Candidate (C4)**: the same protection does **not** extend to `_Generic` / `__builtin_choose_expr` / `BinaryConditional`. Filed as F09 for codegen path; analysis path may have similar gap.

### `BorrowCheckerPrologue::TransformCallExpr` — :786+
**Invariant**: each call argument is replaced by a `_borrowck_tmp_N` DRE. The temp's VarDecl gets pushed into outer Stmts (via `Stmts.push_back(DS)` at :441 in helper).
**Peer**: Epilogue must restore via `replacedNodesMap`. Yes — `TransformCallExpr` in Epilogue iterates args looking up replacements.

### `BorrowCheckerPrologue::TransformCompoundLiteralExpr` — :811
**Invariant**: replaces the compound literal with a DRE to a hoisted temp.

### `BorrowCheckerPrologue::TransformInitListExpr` — :876-889
**Invariant**: transforms each init expr; replaces the entire InitListExpr with a DRE to a hoisted temp.
**Note**: this is *expression-shaped* `(struct S){...}`, not declarator-shaped `struct S s = {...}`.

### `BorrowCheckerEpilogue::TransformStmt` — :965-994
**Invariant**: dispatch by `getStmtClass()`. For statement nodes, call the matching `Transform<Node>`; for expression nodes, call `TransformExpr`. **`AlwaysRebuild()` is false** — does not redo Sema, so AST nodes aren't reconstructed (preserves destructor-insertion bookkeeping).

### `BorrowCheckerEpilogue::Transform*Stmt` (compound, if, do, for, while, switch) — :996-1195
**Invariant**: for each sub-statement / cond / body, look it up in `replacedNodesMap.Contains(...)`; if found, swap to the original; then `TransformStmt` recursively.
**Symmetry**: matches Prologue's overrides one-for-one.

## Top probing candidates (ranked)

1. **C4/_Generic in borrow-position** — neither Prologue nor Epilogue normalizes `_Generic`. F09 covered codegen path; specifically check borrow-check analysis path: `int *_Borrow b = _Generic((char)0, char: &_Mut x, default: &_Mut y);` — does the freeze of x and y get tracked? Without Prologue rewrite, borrow Sources collide.
2. **C4/BinaryConditional** — same for `&_Mut x ?: &_Mut y` (rare, GNU).
3. **C1/Prologue replace temps** — Prologue uses `ReplaceWithRefToNewTempVar(E)`. When this is called on `E = (CallExpr)` vs `E = ParenExpr(CallExpr)`, does the replacement happen on the inner CallExpr or the outer Paren? If the latter, the temp's *type* might be `void` or wrong.

## Not yet read

- `ReplaceWithRefToNewTempVar` — helper at top of Prologue. The wrapper handling here is critical for C1 audit.
- `NewTempVar` — temp VarDecl factory.
- `replacedNodesMap` — the Insert/Get/Contains semantics (does Insert overwrite? does Get follow a chain?).

## Cycle 10: BSCDataflowAnalysis entry, HasSafeZoneInStmt, FindSafeFeatures

### `BSCDataflowAnalysis` (line 267-)

**Invariant**: orchestrates init, nullability, ownership, borrow checks based on flags + safe-zone presence + ensure_init params.

**Reachability**:
- Line 282-289: SafeOnly nullability mode skips check if no safe zone in function — by-design.
- Line 304-323: UninitCheck mode gates init check on safe zone OR ensure_init params (for UC_SAFE) vs always (for UC_ALL).
- `_Owned` ownership analyzer fires regardless (not gated here).

### `HasSafeZoneInStmt` (line 212-240)

**Invariant**: recurses Stmt tree looking for CompoundStmt/SafeStmt/SafeExpr with SZ_Safe specifier.

**Reachability**: only SZ_Safe is detected — SZ_Unsafe ignored (by-design since init check is per-safe-zone). Iterates Stmt children() only; non-Stmt children (decls) skipped (by-design for stmt traversal). No probe-worthy gap.

### `FindSafeFeatures` (line 203-210)

**Invariant**: checks if function uses any `_Owned`/`_Borrow` types — gates borrow check.

**Status**: thin wrapper around SafeFeatureFinder. Not probe-worthy at this level.

## Cycle (Explorer SemaDecl): Redecl-gate predicate audit

### `HasDiffBorrorOrOwnedQualifiers` (:85-101) — UNPROBED

**Invariant**: returns true iff LHS and RHS types disagree on `_Owned`, `_Borrow`, or `_ArrayElem` qualifier — recursively through `PointerType` levels.

**Call site**: `SemaDecl.cpp:4300` — gates homogeneous-redecl compatibility. If it returns false, the redecl is accepted as compatible. If a path through the type sugar evades the check, we get **silent acceptance of two incompatible declarations** — caller of the second decl sees one signature; callee uses the other; runtime UB if `_Borrow` vs raw differs.

**Peers**: `HasDiffNullabilityParamsTypeAtBothFunction` (uses `HasDiffNullabilityQualifiers`, line 78-83 — which calls `getNullability` which DOES recurse through sugar). Asymmetric: nullability uses Type::getNullability (handles sugar); borrow/owned does ad-hoc PointerType recursion.

**Candidates**:
1. **Function-pointer parameter** — `void (*f)(int *_Borrow)` vs `void (*f)(int *)`. Top type is `Pointer<FunctionProto>`. Recursion at line 95 checks pointee = FunctionProto — not pointer — so doesn't recurse further. Inner `_Borrow` is invisible. **C1 asymmetry**: same shape with nullability WOULD be detected via getNullability (which walks function-proto subtypes). RANK: HIGH — clean differential.
2. **Array type with `_ArrayElem`** — `int *_Borrow _ArrayElem` vs `int *_Borrow`. Should be caught at the top-level isArrayElemQualified check (line 92). But what about `int *_Borrow _ArrayElem (*p)[N]` — array of pointers? Probably out-of-grammar in BSC. RANK: MED.
3. **TypedefType / sugar** — `typedef int *_Borrow B; void f(B);` vs `void f(int*);`. `isBorrowQualified()` should see through typedefs in canonical form. RANK: LOW (needs typedef machinery).
4. **Reference / Block pointer / ObjC pointer** — irrelevant for BSC; skip.

## HasDiffNullabilityQualifiers (SemaDeclBSC.cpp:78-83) — CANDIDATE NEW

**Invariant**: returns true iff LHS and RHS have differing nullability qualifiers anywhere in the type chain. Used by `HasDiffNullabilityParamsTypeAtBothFunction` (:129), which gates function-redeclaration compatibility at SemaDecl.cpp:4307.

**Body** (3 lines):
```
Optional<NullabilityKind> LHSNullability = LHSType->getNullability(Ctx);
Optional<NullabilityKind> RHSNullability = RHSType->getNullability(Ctx);
return LHSNullability != RHSNullability;
```

**Defect**: `Type::getNullability` (Type.cpp:4203-4215) only walks outer AttributedType sugar; it does NOT descend through PointerType / FunctionProtoType / etc. For `int *_Nonnull *p` vs `int *_Nullable *p`, both outer types are `Pointer(Pointer(int))` with no top-level nullability attribute → both return None → predicate returns false → mismatch silently merged.

**Peer asymmetry**: `HasDiffBorrorOrOwnedQualifiers` (line 85-101) DOES have a PointerType recursion arm. `HasDiffNullabilityQualifiers` does not. The same family of redecl gates handles owned/borrow at depth but nullability only at outer level.

**Sibling of F53**: F53 (filed) covers `HasDiffBorrorOrOwnedQualifiers` missing FunctionProtoType recursion. The F53 narrative explicitly CLAIMS the nullability peer handles depth correctly via Clang's nullability infrastructure — but that claim is wrong: `getNullability` only walks AttributedType sugar.

**Probe**: `/tmp/explorer_probe.KtbuD2.cbs` — function and variable redecl with pointee nullability mismatch, both silently merged.
**Baseline**: `/tmp/explorer_baseline.tK0Ksu.cbs` — outer-level nullability mismatch correctly diagnosed.

Defect class: C1 (peer-predicate asymmetry, only-outer-level). Distinct fix surface from F41 (CheckOwnedFunctionPointerType) and F53 (HasDiffBorrorOrOwnedQualifiers).

## Chain T — Homogeneous redecl HasDiff* family FULL recursion table (2026-05-30 bsc-explorer) — SATURATED @ 28656aa9

Chain T traced: `MergeFunctionDecl` SemaDecl.cpp:4413 (owned/borrow, homogeneous-only)
+ :4420 (nullability, BOTH homo+hetero) → `HasDiffBorrowOrOwnedParamsTypeAtBothFunction`
(SemaDeclBSC.cpp:103-127) / `HasDiffNullabilityParamsTypeAtBothFunction` (:129-152) →
per-param/return `HasDiffBorrorOrOwnedQualifiers` (:85-101) / `HasDiffNullabilityQualifiers` (:78-83).

**Recursion structure:**
- `HasDiffBorrorOrOwnedQualifiers` (:85-101): compares OUTER owned/borrow/arrayelem
  flags, then recurses ONLY through `isPointerType()` pointees (:95-98). NO FunctionProtoType
  arm, NO ArrayType arm.
- `HasDiffNullabilityQualifiers` (:78-83): `getNullability(Ctx)` at OUTER level ONLY.
  NO recursion of any kind (getNullability walks AttributedType sugar, not Pointer/FnProto).
- Wrapper param-strip asymmetry: owned/borrow params get `.getUnqualifiedType()` (:120)
  which in BSC (Type.h:7103-7124) PRESERVES owned/borrow (re-adds addOwned|addBorrow) — so the
  strip does NOT lose the outer BSC qual. Nullability params (:146) are passed raw (no strip).
  Return types passed raw on both (:111-112, :137-138). This asymmetry is BENIGN (probed).

**Cell table (predicate × dimension × nesting), HOMOGENEOUS redecl:**
| nesting \ dim | owned/borrow | arrayelem | nullability |
|---|---|---|---|
| outer pointer | REJECT (sound) | REJECT (sound) | REJECT (sound) |
| plain-ptr pointee (1 lvl) | REJECT (sound, P4) | REJECT (sound, P1) | **ACCEPT = F56** (filed) |
| plain-ptr pointee (2 lvl) | REJECT (sound, P3) | (sound by induction) | F56 |
| outer + ArrayElem combo | REJECT (sound, P5/P6) | — | — |
| fnptr-PARAM pointee | **ACCEPT = F53** (filed) | F53-fold | **ACCEPT = F56** (filed) |
| fnptr-RETURN pointee | ACCEPT = F53-fold (P8) | F53-fold | ACCEPT = F56-fold (P7) |
| ptr-to-array element | SHAPE-REJECTED (grammar forbids `_Owned` on array elem, P9) | shape-rej | shape-rej |

**VERDICT: NO NEW root cause. Chain T → SATURATED @ 28656aa9.** The homogeneous HasDiff*
family has EXACTLY two gaps, both already filed: F53 (owned/borrow missing FunctionProto
recursion) and F56 (nullability missing ALL recursion — covers plain-ptr AND fnptr). Every
other cell is SOUND (the plain-pointer recursion in HasDiffBorrorOrOwnedQualifiers catches
owned/borrow/arrayelem at every depth) or SHAPE-REJECTED (array-element BSC qual forbidden).
The owned/borrow plain-pointer-nested cell (F77's HETEROGENEOUS hole) is CORRECTLY REJECTED in
the homogeneous path — confirming F77 is hetero-only. fnptr-return positions FOLD into F53/F56
(same predicate, same missing FnProto recursion — one fix closes param+return).
**Reopen-if:** a commit adds a FunctionProtoType/ArrayType recursion arm to either predicate
(may close F53/F56 — re-verify), or touches HasDiffBorrorOrOwnedQualifiers / HasDiffNullabilityQualifiers
/ the getUnqualifiedType BSC special-case (Type.h:7103-7124).

## CheckNullabilityQualTypeAssignment(QualType,QualType) (SemaDeclBSC.cpp:156-186) — UNPROBED

**Invariant**: when assigning a pointer-typed RHS to a pointer-typed LHS, all nested levels of pointee nullability must be compatible. The bad direction is "Nullable→NonNull" (assigning a Nullable RHS pointee to a NonNull LHS pointee leaks the possibility of null).

**Call site**: `SemaExpr.cpp:10334` inside `CheckAssignmentConstraints`, gated on both top types being PointerType. Returns false → IncompatiblePointer + diag.

**Peers**: `HasDiffNullabilityQualifiers` (line 78-83, only checks outer-level — peer of F56). `CheckBSCQualTypeAssignment` (called before, handles `_Owned`/`_Borrow`). `CheckBSCFunctionPointerType` (called for FnPtr types).

**Candidates**:
1. **C1: optional-pair short-circuit (BOTH nullabilities required)** — L171 `if (LHSNullability && RHSNullability)`. If RHS has NO nullability annotation on the pointee (raw `int *` in unsafe zone, or struct field declared without explicit nullability), `getNullability` returns None → check skipped → silent acceptance. Worst case: LHS demands NonNull, RHS pointee has no annotation → assigned without diag → caller dereferences expecting NonNull → null-deref UB. RANK: HIGH (clean differential between annotated and unannotated RHS).
2. **C1: recursion guard requires BOTH pointee to be pointer** — L180 `if (LHSPointee->isPointerType() && RHSPointee->isPointerType())`. If one side is `int *_Nonnull` and the other is e.g. `void *_Nullable` (where the pointee on one side ISN'T a pointer because it's a function/array — actually no, void/int are not pointers), recursion stops. The current-level check already fired at L171, so the recursion is only for DEEPER levels. Unlikely to produce a fresh bug.
3. **C1: recursion drops accumulated state** — L181-182 `return CheckNullabilityQualTypeAssignment(LHSPointee, RHSPointee)` — only returns the inner recursive call's result. If outer-level had already determined incompatibility, L175 already returned. So the structural symmetry is: each level's check is independent. The defect would be missing-check at SOME level (i.e., candidate 1). RANK: LOW.
4. **C1: BSC's `_Nullable`/`_Nonnull` vs Clang's nullability** — `getNullability` walks AttributedType chain. If BSC's `_Nullable` synthesizes a different attribute kind, the walk may not see it. RANK: MED, but would manifest as ALL nullability checks failing — would have been noticed already.

Top candidate: #1 (RHS lacks nullability annotation entirely, LHS demands NonNull on pointee).

**CONFIRMED-new (2026-05-21 Explorer)**: `int **` -> `int *_Nonnull *` (LHS pointee NonNull, RHS pointee unannotated) silently accepted. Runtime SIGSEGV. The L171 `if (LHSNullability && RHSNullability)` short-circuits when RHS pointee returns `None` from `getNullability` (which it does for unannotated raw pointers). Same-shape baseline with RHS pointee explicitly `_Nullable` correctly diagnosed `error: nonnull pointer cannot be assigned by nullable pointer`.

- Probe: `/tmp/explorer_probe.3e8Cxc.cbs` (compile clean, run SIGSEGV)
- Baseline: `/tmp/explorer_baseline.u8zWAI.cbs` (compile rejected)

Defect class: **C1 (Ignore-asymmetry, but at the OPTIONAL-pair short-circuit level)**. Distinct from F56 (HasDiffNullabilityQualifiers — function-redecl gate, outer-only). The fix at L171 should treat None on either side as "unknown — conservatively reject if the other side is NonNull" in safety-mode-on context. Blast radius: every place using `if (LHSNullability && RHSNullability)` to gate a compatibility check has the same shape.

## BorrowCheckerPrologue loop transforms (SemaDeclBSC.cpp:640-795) — 2026-05-29
**Invariant**: for/while/do-while wrap the COND (and for-INC) in a compiler-
generated `StmtExpr` `({ x = cond; x; })` so that temp-hoisting done by inner
transforms stays IN-PLACE (re-evaluated each iteration), not hoisted before the
loop. if/switch conds use direct TransformExpr (single eval, hoist-before OK).
**Peers**: TransformBinaryOperator (:813, F40 eager-hoist of &&/|| RHS),
TransformConditionalOperator (:859, builds IfStmt — conditional preserved).
**Candidates**:
1. **for-INC with a consuming expr** (`for(;cond; safe_free(p))`): inc runs each
   iteration; iteration 2 = double-free. Is the loop back-edge re-move caught? UNPROBED → probing.
2. `&&`/`||` inside a LOOP cond — StmtExpr-wrapping contains the F40 hoist
   per-iteration; same root as F40 (TransformBinaryOperator). FOLDED-F40.
3. TransformArraySubscriptExpr (:797) does NOT temp-wrap the subscript (unlike
   CStyleCast/CallExpr/BinaryOperator) — lvalue kept in place; reasoned-safe.

## BorrowCheckerEpilogue::TransformSwitchStmt (SemaDeclBSC.cpp:1236-1244) — C4 asymmetry, 2026-05-29
**Invariant**: the Epilogue must restore every Prologue-substituted node (temp
DeclRefExpr) back to the original expr via replacedNodesMap, at every control-flow
position, so codegen sees the original AST (not an undefined `_borrowck_tmp`).
**Asymmetry (candidate, C4)**: TransformSwitchStmt does `SS->setCond(TransformExpr(
SS->getCond()))` with NO `replacedNodesMap.Contains/Get` restore on the cond —
UNLIKE TransformIfStmt(:1177), TransformWhileStmt(:1249), TransformForStmt cond(:1150),
TransformDoStmt cond(:1131) which ALL restore. If the Prologue substituted the switch
cond (BinaryOperator :822 / non-void CallExpr :840 / CStyleCast :903), the temp DRE
stays in the final switch cond → F09-class undefined-temp leak to codegen/rewriter.
**Probe outcome (2026-05-29): PROBED-BENIGN (not filed).** Real code asymmetry but NO observable defect: runtime correct for binop cond (`100 200 999`) and non-void-call cond (`100`); side-effecting cond `switch(tick()+*b)` evaluates exactly ONCE (calls=1, no double-eval); codegen clean. Benign because the Prologue hoists `tmp=cond` + sets `switch(tmp)`; the Epilogue not restoring just leaves the equivalent `tmp=cond; switch(tmp)` form (semantically identical, single eval). Worth a maintainer tidy (latent C4 inconsistency vs if/while/for/do which DO restore) but no FP/FN/miscompile.

## CheckNullabilityQualTypeAssignment(QualType,QualType) (SemaDeclBSC.cpp:170) — nested-pointer nullability assign check (2026-06-27)
INVARIANT: recursively rejects assigning a Nullable pointee to a NonNull pointee across nested pointer levels — returns
false (incompatible) iff at some level LHSPointee nullability==NonNull AND RHSPointee nullability ∈ {Nullable,NullableResult}.
POST-REVERT (c2962872, 回退 PR!858): LHSNullability comes ONLY from explicit ->getNullability (no Owned/Borrow→NonNull
inference); RHSNullability from explicit annotation OR unannotated-pointer-pointee→Nullable. PEERS: the (QualType,Expr*)
wrapper [:204, emits err_nonnull_assigned_by_nullable], HasDiffNullabilityQualifiers, getDefNullability (BSCNullabilityCheck
DOES infer Owned/Borrow→NonNull — asymmetric with this post-revert).
CANDIDATES:
1. (LHS Owned/Borrow pointee not inferred NonNull) = F66 RE-OPENED by revert: int*_Borrow *p = nullable-pointee-RHS slips
   (LHSNullability=None, guard skipped). PROBED-confirmed-F66 (deliberate revert → DO-NOT-FILE).
2. (boundary: explicit nested nullable→nonnull still caught?) revert removed only Owned/Borrow inference; EXPLICIT
   int*_Nonnull * ← int*_Nullable * STILL rejected (exit=1) — PROBED-sound; revert blast radius = F66 only.
3. (3-level recursion int***) recursion checks each level (depth-2 mismatch caught exit=1) — PROBED-sound.

## HasDiffNullabilityQualifiers / HasDiffBorrorOrOwnedQualifiers (SemaDeclBSC.cpp:80-114) — homogeneous-redecl variance checks
- **Invariant**: a homogeneous (same-zone) function redecl with a differing owned/borrow OR nullability qualifier (on return or any param, INCLUDING nested fnptr params) must be rejected `err_conflicting_types` (SemaDecl.cpp:4440 owned / :4447 nullability).
- **Candidates**: (1) **PROBED-confirmed-F125 (HIGH, FILED)**: `HasDiffNullabilityQualifiers` (:80-85) is TOP-LEVEL ONLY (`getNullability()` of the fnptr pointer itself), while `HasDiffBorrorOrOwnedQualifiers` (:87-114) RECURSES into FunctionProtoType pointee params/return (:100-111) — so a nested-fnptr-param nullability redecl mismatch slips (accepted) while the owned analog + direct-param nullability are rejected → null-deref laundering. Fix: recurse nullability. (2) UNPROBED: does `HasDiffNullabilityQualifiers` also miss nested nullability in a POINTER-to-pointer param (`int *_Nonnull *` vs `int *_Nullable *`)? getNullability of `int**` is top-level none for both → may also slip (but F66 covers the assignment side; redecl side untested). (3) `isArrayElemQualified` diff (:94) is checked for owned but not nullability — array-elem nullability redecl untested.
