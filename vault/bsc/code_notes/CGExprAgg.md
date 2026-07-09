# clang/lib/CodeGen/CGExprAgg.cpp — BSC coverage notes

## AggExprEmitter visitor (CGExprAgg.cpp:80-200)

**Invariant**: every Expr kind that can have an aggregate type (struct/union/array result) must have a corresponding `Visit*` override in `AggExprEmitter`. The fallback is `VisitStmt(Stmt *S) { CGF.ErrorUnsupported(S, "aggregate expression"); }` at line ~110-112, which prints "cannot compile this aggregate expression yet" and continues with broken IR.

**Peers**:
- `clang/lib/CodeGen/CGExprScalar.cpp` ScalarExprEmitter (handles scalar-typed exprs; HAS `VisitSafeExpr` at :452)
- `clang/lib/CodeGen/CGExpr.cpp` EmitLValue switch (HAS `case Expr::SafeExprClass` at :1368)
- `clang/lib/CodeGen/CGExprComplex.cpp` ComplexExprEmitter (no SafeExpr handler)
- `clang/lib/CodeGen/CGExprConstant.cpp` ConstExprEmitter (no SafeExpr handler)

## VisitSafeExpr — MISSING (**CONFIRMED-new, F60-pending**)

**Invariant**: `_Safe(E)` / `_Unsafe(E)` (parsed as a `SafeExpr` AST node) is a transparent wrapper — codegen must recurse into the sub-expression regardless of the result type (scalar / aggregate / complex / constant).

**Peers**:
- CGExprScalar.cpp:452 has `Value *VisitSafeExpr(SafeExpr *E) { return Visit(E->getSubExpr()); }` — scalar emitter is correct.
- CGExpr.cpp:1368 has `case Expr::SafeExprClass: return EmitLValue(cast<SafeExpr>(E)->getSubExpr());` — lvalue path is correct.
- CGExprAgg.cpp — NO `VisitSafeExpr` override. Falls through to `VisitStmt` (line ~110-112), which calls `ErrorUnsupported(S, "aggregate expression")`.
- CGExprComplex.cpp — same gap, but `_Complex` struct fields under `_Safe(...)` is a less common shape.

**Confirmed symptom**: `_Safe(mk())` where `mk()` returns `struct S` by value triggers `error: cannot compile this aggregate expression yet`. The scalar baseline `_Safe(scalar_call())` works.

**FOLDED variants** (all hit the same CGExprAgg gap):
- `_Safe(struct_returning_fn())` — CallExpr returning struct
- `_Safe((struct S){.f = 1, ...})` — compound literal
- `_Unsafe(struct_returning_fn())` — same gap (Unsafe and Safe share the SafeExpr node)
- `_Safe(arr_returning())` — function returning array would behave similarly (array-result also `TEK_Aggregate`)

**Fix surface**: in CGExprAgg.cpp around line 113-127 (where `VisitParenExpr` and other transparent wrappers live), add:
```cpp
#if ENABLE_BSC
void VisitSafeExpr(SafeExpr *E) { Visit(E->getSubExpr()); }
#endif
```

**Defect class**: C3 — Visit/switch coverage gap. The transparent-wrapper SafeExpr has handlers in the scalar and lvalue codegen paths but not in the aggregate codegen path. Same shape as F09 (rewriter missing ChooseExpr handler) but on the aggregate-emitter dispatch.

**Severity**: MEDIUM. Translation-fidelity defect. Code that compiles under `-fsyntax-only` fails at actual codegen. The user-visible symptom is a compiler-internal-sounding "cannot compile this aggregate expression yet" error rather than a soundness hole. No silent miscompile; the diag fires loudly. But: valid BSC source is rejected by codegen, blocking compilation of programs that use `_Safe(expr)` on aggregate-typed expressions.

## ConstExprEmitter (CGExprConstant.cpp:1015) missing VisitSafeExpr — PROBED-confirmed-F69 (2026-05-29)
Completes the codegen SafeExpr-coverage audit. Status of every emitter:
- ScalarExprEmitter (CGExprScalar.cpp:452) — HAS VisitSafeExpr. OK.
- EmitLValue (CGExpr.cpp:1368) — HAS case SafeExprClass. OK.
- AggExprEmitter (CGExprAgg.cpp) — MISSING → **F60** (runtime aggregate; "cannot compile this aggregate expression yet").
- ComplexExprEmitter (CGExprComplex.cpp) — MISSING → **F63** (runtime _Complex).
- **ConstExprEmitter (CGExprConstant.cpp:1015) — MISSING → F69** (CONSTANT/static-initializer path; "cannot compile this static initializer yet"). Scalar static masked by APValue fallback; aggregate static errors. Distinct file/function/fix from F60/F63. Repro `repro/F69_safeexpr_static_aggregate_initializer.cbs`.
The codegen SafeExpr-unwrap surface is now fully mapped: scalar+lvalue OK, three emitters (agg/complex/const) each filed separately.

## CGExprConstant missing SafeExpr (F60/F63 class) — candidate 2026-06-17
INVARIANT: every CodeGen expr-emitter that walks the expr tree must handle SafeExpr (transparent wrapper) or it drops/mishandles the wrapped expr. CGExprScalar/Agg/Complex/CGExpr handle it (F60=Agg, F63=Complex). CGExprConstant.cpp = 0 SafeExpr mentions.
Candidates:
1. [coverage C3] `_Safe(const)` in a CONSTANT context → **PROBED-CONFIRMED-G09 (2026-06-17)**: SCALAR-wrapped constant (global int / array-size / enum) is FINE; only AGGREGATE-wrapped (struct/array compound literal) as a global/static-local init fails — `ConstExprEmitter` (CGExprConstant.cpp:1015) lacks VisitSafeExpr (0 occurrences). INCOMPLETE-FIX sibling of F60/F63 (a9deb1b fixed AggExprEmitter+ComplexExprEmitter but missed ConstExprEmitter). Distinct fix site. MEDIUM FP, repro G09. Not yet filed (user decision).
2. [composition] `_Safe(const)` in a designated/compound-literal constant init. UNPROBED

## CodeGen SafeExpr coverage — F60(Agg)/F63(Complex) FIXED; CGExprConstant gap? (2026-06-24)
**Status**: VisitSafeExpr now present in CGExprAgg:129, CGExprComplex:116, CGExprScalar:452, CGExpr(lvalue):1368
→ F60/F63 FIXED. CGExprConstant.cpp has NO SafeExpr mention.
**Candidate**: SafeExpr-wrapped expr reaching the CONSTANT emitter (static/const initializer in _Safe context)
→ if CGExprConstant doesn't strip/handle SafeExpr → wrong constant / crash. Likely the const-EVALUATOR
(ExprConstant) strips SafeExpr before codegen, so unreachable — verify by value-check. UNPROBED ⭐.
