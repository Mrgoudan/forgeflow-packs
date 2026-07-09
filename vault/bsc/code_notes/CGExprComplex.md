# CGExprComplex.cpp — notes

Source: `clang/lib/CodeGen/CGExprComplex.cpp`. CodeGen entry for expressions
producing `_Complex` results. Visitor class `ComplexExprEmitter` is a
`StmtVisitor<ComplexExprEmitter, ComplexPairTy>` (line 45).

Dispatch shape (line 95+):
- `Visit(Expr*)` → `StmtVisitor::Visit` walks the AST node kind table.
- `VisitStmt` → `llvm_unreachable("Stmt can't have complex result type!")`.
- `VisitExpr` (line 395) — fallback — emits
  `CGF.ErrorUnsupported(E, "complex expression")` then returns `(undef, undef)`.

Wrapper-recurse style overrides present (all forward to sub-expr):
- `VisitParenExpr` (line 111)
- `VisitGenericSelectionExpr` (line 112)
- `VisitConstantExpr` (line 105) — first tries constant-emit, then recurses
- `VisitUnaryPlus` (line 209) — clears flags then recurses
- `VisitUnaryExtension` (line 217)
- `VisitCXXDefaultArgExpr` (line 220)
- `VisitCXXDefaultInitExpr` (line 224)
- `VisitExprWithCleanups` (line 228)

NOT present: `VisitSafeExpr`. This is the BSC-specific AST node created by
the `_Safe(...)` / `_Unsafe(...)` expression form (`Parser::ParseSafeExpression`
in `ParseExprBSC.cpp`). Peer file `CGExprScalar.cpp:452` HAS the override:
```cpp
Value *VisitSafeExpr(SafeExpr *E) { return Visit(E->getSubExpr()); }
```
Peer file `CGExpr.cpp:1368` handles SafeExpr in the lvalue switch. Peer file
`CGExprAgg.cpp` is missing the override (F60, FILED). The third sibling
emitter — `ComplexExprEmitter` — is also missing it.

## ComplexExprEmitter (CGExprComplex.cpp:45-395) — CONFIRMED-new

**Invariant**: Any AST node with `_Complex` result type that is a transparent
single-child wrapper SHOULD recurse into its child rather than fall through
to the unsupported-expression error path.

**Peers**:
- `AggExprEmitter` (CGExprAgg.cpp) — F60 confirms same root cause for aggregate types.
- `ScalarExprEmitter` (CGExprScalar.cpp:452) — has `VisitSafeExpr`. Correct.
- `EmitLValue` SafeExpr arm (CGExpr.cpp:1368) — has the case. Correct.

**Candidates**:
1. **`VisitSafeExpr` override missing — CONFIRMED-new** (2026-05-21 Explorer).
   `_Safe(<_Complex-typed expr>)` and `_Unsafe(<_Complex-typed expr>)` both
   reach `ComplexExprEmitter::Visit` (since `_Complex` is the result type),
   fall to `VisitExpr`, and emit
   `error: cannot compile this complex expression yet`. Confirmed with both
   `_Safe(call_returning_double_complex())` and `_Unsafe(complex_lvalue)`.
   Repro: `/tmp/explorer_probe.cOFTWL.cbs`. Baseline (no SafeExpr wrapper):
   `/tmp/explorer_probe.DUVdMA.cbs` — runs r=3 i=4. Sibling: `/tmp/explorer_probe.cRElY7.cbs`
   (`_Unsafe(c0)` non-call form) — same error.
   Distinct from F60 by file/visitor class/fix surface (separate override
   needed on `ComplexExprEmitter`, not on `AggExprEmitter`).
   Defect class: **C3** (Visit/switch coverage gap in CodeGen complex dispatch).
   Fix surface (1-liner):
   ```cpp
   ComplexPairTy VisitSafeExpr(SafeExpr *E) { return Visit(E->getSubExpr()); }
   ```

2. **`VisitStmtExpr` (line 188 — declaration only)** — UNPROBED. Out of scope
   (`({...})` is GNU and on the keyword block list).

3. **`VisitOpaqueValueExpr` (line 163)** — UNPROBED. Could BSC's rewriter
   shove an OpaqueValueExpr into the Complex path without a mapping? Unlikely
   given BSC doesn't use opaque-value heavily, but check before retiring.
