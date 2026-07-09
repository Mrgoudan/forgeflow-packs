# CGExprConstant.cpp — note

Source: `clang/lib/CodeGen/CGExprConstant.cpp`. Two visitor classes:
- `ConstExprEmitter : StmtVisitor<…, llvm::Constant*, QualType>` (:1015) — emits an
  `llvm::Constant*` directly from an expr tree for an aggregate/structured initializer.
- `ConstantLValueEmitter : ConstStmtVisitor<…, ConstantLValue>` (:1786) — emits an
  address constant for a pointer/lvalue initializer.

## Invariant
`ConstExprEmitter` returns a non-null `llvm::Constant*` iff the expr is a compile-time
constant emittable as IR initializer data; otherwise returns `nullptr`
(default `VisitStmt`, :1029) → caller falls back to the APValue path / runtime init.
A `nullptr` is SOUND (conservative): it never miscompiles, it only declines.

## Peers
- `CGExprAgg.cpp` AggExprEmitter (has `VisitSafeExpr`, F60 fix), `CGExprComplex.cpp`
  ComplexExprEmitter (has `VisitSafeExpr`, F63 fix), `CGExprScalar.cpp` ScalarExprEmitter
  (has `VisitSafeExpr`). **`CGExprConstant.cpp` is the ONE emitter with NO `VisitSafeExpr`
  and no `SafeExpr` mention.**
- `ExprConstant.cpp` (AST constant evaluator / `Evaluate`) — DOES handle `SafeExpr`
  (in its caller list). This is the firewall (see candidate 1).

## Candidates

1. **`ConstExprEmitter` missing `VisitSafeExpr` → wrong/declined constant for a
   `_Safe(expr)`/`_Unsafe(expr)` in a constant-required position.**
   `SafeExpr` (from `_Safe (paren-expr)` / `_Unsafe (paren-expr)`, `ActOnSafeExpr`,
   SemaStmtBSC.cpp:41) is unwrapped by `ParenExpr`/`ConstantExpr`/`ExprWithCleanups`
   peers but NOT by a `VisitSafeExpr`; default `VisitStmt` returns `nullptr`.
   → **PROBED-SOUND (latent-unreachable) @808187e6.** The APValue path
   (`ExprConstant.cpp`, which handles SafeExpr) folds the initializer BEFORE
   `ConstExprEmitter` is consulted, so the missing visitor is never the deciding path.
   Probe `ce1.cbs`: `const int Gscalar=_Safe(5+2); int Garr[2]={_Safe(1),2};
   int Gsize[_Safe(4)]; enum{E=_Unsafe(3)};` → compile rc=0; IR
   `@Gscalar=constant i32 7`, `@Garr=[i32 1,i32 2]`, `@Gsize=[4 x i32]` — all correct.
   Same shape/outcome as WalkerBSC `SafeFeatureFinder::VisitQualType` latent-unreachable.

2. **`ConstantLValueEmitter` (:1786) missing `VisitSafeExpr` for an ADDRESS constant**
   wrapped in `_Safe(&g)`. UNPROBED, but expected same firewall as candidate 1
   (ExprConstant's lvalue-base evaluation handles the wrapper). LOW priority.

3. **InitListExpr aggregate where the whole list is non-foldable by APValue but one
   element is `_Safe(...)`** — would force the ConstExprEmitter element walk. UNPROBED;
   hard to construct a list that fails APValue yet stays a valid constant, so likely
   shape-rejected. LOW priority.
