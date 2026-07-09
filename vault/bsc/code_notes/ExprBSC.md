# ExprBSC.cpp — BSC Expression AST predicates

Source: `clang/lib/AST/BSC/ExprBSC.cpp` (55 lines). UNMAPPED before 2026-06-23.
Single function: `Expr::isNullExpr(ASTContext &)` (:26-55).

## isNullExpr (ExprBSC.cpp:26-55) — PROBED-confirmed-F108
**Invariant**: returns true iff the expression evaluates to a null pointer constant
(nullptr / 0 / (void*)0 / nested cast-of-null / paren-of-null / the 4 raw-transfer
builtins applied to a null arg / any getIntegerConstantExpr==0). Used by analyzers as
the "this value is null → no ownership / nullable" predicate.
**Implementation**: a `dyn_cast` ladder over IntegerLiteral / CStyleCastExpr /
ImplicitCastExpr / ParenExpr / CallExpr(4 builtins) / getIntegerConstantExpr fallback.
NO arm for: ConditionalOperator, BinaryOperator (e.g. `&a - &a`), MaterializeTemporary,
CStyleCastExpr to pointer of a null *integer* is handled (recurses into sub), but a
cast whose sub is itself a ConditionalOperator still bottoms out at "not null".

**Peers** (the same null-classification question answered by a SECOND function with a
RICHER switch — the asymmetry):
- `BSCNullabilityCheck.cpp:307-313` `getExprPathNullability`: calls `isNullExpr` FIRST
  (→Nullable if true), then has its OWN `ConditionalOperatorClass` arm (line ~345) that
  returns Nullable when EITHER branch is Nullable. So `(c?nullptr:nullptr)` is Nullable
  to the nullability checker.
- `BSCOwnership.cpp:2201` (BinAssign RHS null → `setToNull`), `:2358` (Decl init null →
  `setToNull`), `:2401` (InitList field null) — these call `isNullExpr` DIRECTLY with
  NO conditional-operator fallback. So the SAME expression is "Nullable" to nullability
  but "Owned (non-null)" to ownership/init.
- `BSCNullCheckInfo.cpp:181-182` (binary `==`/`!=` null compare) — also direct.

**Downstream consequence of the peer gap**: when an `_Owned` pointer is initialized/
assigned a null-valued expr that `isNullExpr` does NOT pattern-match, ownership leaves
it in `Owned` state (not `Null`). At scope exit, `checkMemoryLeak` (:1941-1958) gates on
`canAssign` (:417) which returns true (no-leak) ONLY if the status bits are a subset of
{Uninitialized,Moved,Null}; `Owned` is NOT in that set → `canAssign` false → spurious
**MemoryLeak false-positive** for code that is provably null-assigned.

**Candidates** (ranked):
1. (C2 opcode-switch hole / peer asymmetry) `_Owned` pointer init/assign via a
   `ConditionalOperator` whose branches are null (`(c?nullptr:nullptr)` or the realistic
   `c?nullptr:nullptr` else-arm). `isNullExpr` has no ConditionalOperatorClass arm →
   returns false → ownership `setToOwned` (NOT setToNull) → `checkMemoryLeak` emits a
   spurious "memory leak" FP. The nullability checker (richer switch) classifies the
   same expr as Nullable → peer disagrees → MODE-2 chain handoff (isNullExpr ×
   getExprPathNullability). TOP — deterministic FP, minimal repro.
2. (C2) `BinaryOperator` producing a null pointer constant via pointer arithmetic, e.g.
   `&a - &a` (yields a 0 pointer of ptrdiff_t, then cast to pointer) — `isNullExpr` has
   no BinaryOperator arm; only the getIntegerConstantExpr fallback MIGHT catch a folded
   `0`. If not folded (e.g. `&a - &a` where `a` is a runtime arg array — but array addr is
   constant; for a VLA or parameter decay it may not fold) → not-null → same leak FP.
   Lower probability the constant-folder misses it; probe to confirm.
3. (C1 ignore-asymmetry) The 4 raw-transfer builtins recurse ONE arg; a user-defined
   function that RETURNS null (e.g. `_Owned f() { return 0; }`) called as the RHS of an
   `_Owned` init: `isNullExpr` CallExpr arm only matches the 4 builtins, returns false
   for user calls → `setToOwned` → leak FP. BUT this is likely deduped by
   `getExprPathNullability`'s CallExpr arm which calls `getDefNullability(CE->getType())`
   (returns the declared return-type nullability, not "this call returns null") — so the
   nullability checker ALSO doesn't see it as null; the two peers AGREE here (both
   not-null) → NOT an asymmetry, just a shared limitation. Lower priority / likely
   SHAPE-rejected-by-design (returning a null _Owned is itself suspect). Probe only if #1 folds.
