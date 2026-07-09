# Defect-Class Playbook

Named defect classes seen in BSC analyzer code. Each class is a **grep-checkable rule** plus an exemplar. Use to classify candidates from invariant-driven reading. Add a class when ≥2 incidents back it.

## Status legend

- **CONFIRMED (filed)**: ≥1 filed Fxx exemplar. **Stop probing variants of the same root cause** — one fix at the root resolves all symptoms. Only investigate if a candidate points to a *new* code site (different file, different function, different invariant).
- **OPEN**: candidates exist in code_notes but no filing yet — fair game to probe.

## Status by class

| Class | Status | Filed exemplars |
|-------|--------|-----------------|
| C1 Ignore-asymmetry | CONFIRMED | F14 (IJOAO8), F17 (IJOERU), F39 (IJONYD) — DefUse::VisitArraySubscriptExpr missing. F46 (IJOSI3) HIGH — `getMemberFullField` strips ImplicitCastExpr but not ParenExpr; `(s).b` paren-wrap bypasses move tracking, runtime double-free |
| C2 Opcode-switch hole | CONFIRMED | F11 (IJO88T) |
| C3 Visit/switch coverage gap | CONFIRMED | F09 (IJO88R), F18 (IJOEWJ). F47 (IJOSRL) HIGH — `CheckTemporaryVarMemoryLeak` recognized-temp set excludes CompoundLiteralExpr; member access on compound-literal-with-owned-field silently leaks |
| C4 Prologue/Epilogue asymmetry | CONFIRMED | F09 (IJO88R) codegen + F16 (IJOEJP) analysis + F40 (IJOPPE) short-circuit/ternary hoisting |
| C5 Dataflow merge state hole | CONFIRMED | F26 (IJOK0G) — mergeDPVD asymmetric meet; mergeVD/mergeFP same shape but protected by initStatus pre-population. F44 (IJOSGF) — `checkSUse`/`isAddrMut` collapses per-field Null state into Owned (single-statement collapse, not cross-branch merge). F45 (IJOSHF) HIGH — `checkSFieldAssign` doesn't erase from SNullOwnedFields when reassigning a previously-null field; stale entry combined with F44 migration produces runtime double-free |
| C6 Localized check skipped on wrappers | CONFIRMED | F13 (LOW, not filed); F12 (IJO88U) — narrow flow variant |
| C7 Narrowing not propagated across join | CONFIRMED | F12 (IJO88U) |
| C8 Path-identity aliasing gap | CONFIRMED | F42 (IJOQGO) — borrow checker's string-based path comparison doesn't model union aliasing |
| C9 Type-recursion gap (FunctionProto / nested) | CONFIRMED | F41 (IJOPV7) owned-fnptr outer-level only; F53 redecl `HasDiffBorrorOrOwnedQualifiers` walks PointerType pointee but not FunctionProtoType params; F56 `HasDiffNullabilityQualifiers`; F57 `AreOwnedBorrowQualifiersCompatible`. A type-walking predicate recurses through pointer pointees but stops at function-proto param/return types — qualifiers nested in a fnptr param are invisible. |
| C10 SafeExpr-strip gap | CONFIRMED | F62 (HIGH, move-through-borrow), F65 (null-narrowing). `IgnoreParenCasts()`/`IgnoreParenImpCasts()` peel the standard wrapper set from `IgnoreExpr.h` but NOT BSC's `SafeExpr` (`_Safe(...)`/`_Unsafe(...)`). Any check that strips wrappers then dispatches on the unwrapped node mis-handles a SafeExpr-wrapped operand. |
| C11 Codegen emitter missing VisitSafeExpr | CONFIRMED | F60 (aggregate), F63 (complex). A `CGExpr*Emitter` (AggExprEmitter, ComplexExprEmitter) lacks a `VisitSafeExpr` override, so `_Safe(<typed-expr>)` falls to the unsupported-expression fallback. Scalar emitters got the recurse-into-subexpr override; aggregate + complex did not. |
| C12 Codegen drops side-effecting builtin arg | CANDIDATE | F58 — `BI__assume_initialized` handler returns a no-op without emitting its argument; side effects in the arg vanish. One incident; promote to CONFIRMED on a second. |

**Rule**: when probing a new candidate, first check what class it belongs to. If the class is CONFIRMED and the candidate is a sibling site / variant of an existing exemplar (same file, same function, same invariant) — drop it; don't probe. Probe only if the candidate is a different root cause.

## Classes

### C1 — Ignore-asymmetry

**Signature**: `dyn_cast<X>(E)` or `isa<X>(E)` without `IgnoreParens()` / `IgnoreImpCasts()` / `IgnoreParenCasts()`, where a peer site uses one of those strips.

**Symptom**: paren-wrap, cast-wrap, etc. bypass the special-case path, falling to a generic handler that does the wrong thing (or nothing).

**Exemplar**: **F14** — `CheckTemporaryVarMemoryLeak` uses `dyn_cast<CallExpr>(E)` while the peer `CheckMoveVarMemoryLeak` uses `IgnoreParenCasts()`. `(void)f(p)` / `(f(p))` slip past the leak check.

**Audit method**: grep `dyn_cast<.*Expr>` and `isa<.*Expr>` in all `clang/lib/Analysis/BSC/*` and `clang/lib/Sema/BSC/*`. For each hit, ask: does the input expression come from a context that can wrap it?

### C2 — Opcode-switch hole

**Signature**: `switch` on `BinaryOperatorKind` / `UnaryOperatorKind` / `CastKind` that doesn't handle every relevant case. Common omissions: `BO_Comma`, `BO_LAnd`, `BO_LOr`, `BO_PtrMemD`, `BO_PtrMemI`, `UO_Extension`, `UO_Real`, `UO_Imag`.

**Symptom**: sub-expression with a side effect is skipped on the omitted op; move / borrow / use-after-move not recorded.

**Exemplar**: **F11** — `DefUse::VisitBinaryOperator` and `ActionExtract::VisitBinaryOperator` lack a `BO_Comma` case → `(f(p), 0)` slips past use-detection.

**Audit method**: every `switch` on an operator kind enum; cross-check against `clang/include/clang/AST/OperationKinds.def`.

### C3 — Visit* coverage gap

**Signature**: a `Visit*` class explicitly overrides handlers for some `Stmt::Class` kinds; everything else falls through to `VisitStmt` (generic child iteration).

**Symptom**: an AST kind that carries side effects but lacks an override; analyzer treats sub-effects as if they don't exist.

**Exemplar**: **F09** — `BSCIRBuilder` lacks `VisitChooseExpr` / `VisitGenericSelectionExpr`; selected branches with side effects are silently dropped at codegen.

**Audit method**: for each `Visit*` class, list explicit overrides; cross-reference against the set of side-effect-carrying AST kinds (call, assign, ++/--, _Mut/_Const borrow, compound literal, statement expr).

### C4 — Prologue/Epilogue asymmetry

**Signature**: `BorrowCheckerPrologue::Transform_X` exists but `BorrowCheckerEpilogue::Transform_X` does not (or vice versa). The Prologue's job is to rewrite AST for analyzability; the Epilogue's job is to restore the source-level AST. Mismatch = AST inconsistency after borrow check.

**Symptom**: post-borrowcheck AST has injected temps / IfStmts that downstream consumers (rewriter, codegen, AST printer) didn't expect.

**Exemplar**: `TransformConditionalOperator` is in Prologue (rewrites `?:` to IfStmt+temp) but not in Epilogue. Currently load-bearing: works because Epilogue uses generic restoration via `replacedNodesMap`. But neither has `TransformChooseExpr` / `TransformGenericSelectionExpr` / `TransformBinaryConditionalOperator` → those AST kinds skip Prologue normalization → side-effect-bearing branches drop.

**Audit method**: diff Prologue's explicit `Transform_X` overrides vs. Epilogue's. Pair Set ↔ Restore explicitly.

### C5 — Dataflow merge state hole

**Signature**: `merge()` produces a status the analyzer's downstream predicates don't discriminate. Status is encoded as a bit-lattice; merging via OR produces multi-bit states; `is(VD, S)` requires exactly one bit set. Some multi-bit states have no handler.

**Symptom**: post-merge state silently allows / rejects an operation that one of the input paths should have disallowed / allowed.

**Exemplar (candidate, unconfirmed)**: `Null|Owned` reachable when one path null-assigns and another owns. `is(VD, Null)` returns false. Does any downstream check rely on Null-discrimination that this state slips past?

**Audit method**: enumerate the 2^6 = 64 status-bit subsets. Mark which are reachable (which `set*` functions produce them, which merges create them). For each reachable multi-bit state, list which downstream predicates it activates / doesn't.

### C6 — Localized check skipped on wrapped sub-expression

**Signature**: a specific Visit handler runs a narrow check (e.g. deref null-check); the check doesn't recurse through a wrapper, or runs only on a single opcode.

**Symptom**: wrapping the dereferenced pointer in a re-borrow / cast / paren skips the check.

**Exemplar**: **F13** — reborrow skips the `*` deref-nullability check.

**Audit method**: for every check that's gated on a specific `UO_Deref` / `UO_AddrOf` / `MemberExpr->isArrow()`, ask: does the check still fire if the operand is wrapped?

### C7 — Conservative narrowing not propagated across join

**Signature**: nullability / state narrowing is established in a branch (e.g. `if (p)` → p Nonnull) but not propagated through `?:` or merge points.

**Symptom**: after a ternary that null-checks in both arms, the result is still treated as `_Nullable` even though both arms produced Nonnull.

**Exemplar**: **F12** — ternary doesn't narrow nullability when both arms confirm non-null.

**Audit method**: trace state-update flows. Wherever a state changes inside a branch, find the join point and check whether the join preserves the narrowing.

## Key load-bearing invariant: Prologue hoisting

`BorrowCheckerPrologue` (SemaDeclBSC.cpp:434) hoists CallExpr, prvalue UnaryOperator, CompoundLiteralExpr, InitListExpr, CStyleCastExpr, ConditionalOperator into temp `_borrowck_tmp_N` DeclStmts that become outer Stmts. Each is recorded in `replacedNodesMap`.

**This is the reason `ActionExtract::VisitArraySubscriptExpr` (and similar) doesn't recurse into all children** — by the time ActionExtract runs, side-effect-bearing children are guaranteed to be simple DREs to hoisted temps. The temps' inits run as their own CFG elements where ActionExtract / DefUse process them.

**Defect-hunt corollary**: any AST kind that **escapes** Prologue hoisting *and* carries side effects is a hot spot. Known escapees:

- `ChooseExpr` (`__builtin_choose_expr`) — no `TransformChooseExpr` in Prologue
- `GenericSelectionExpr` (`_Generic`) — no `TransformGenericSelectionExpr`
- `BinaryConditionalOperator` (GNU `a ?: b`) — no override
- `AtomicExpr`, `VAArgExpr`, `OpaqueValueExpr`, `PseudoObjectExpr` — no overrides

Each of these is a C4 candidate. F09 found this in codegen; analysis path is likely affected too.

## How to add a class

Append a new section with the same template once a violation pattern has ≥2 incidents (across issues, commits, or fresh discoveries). One incident = candidate; two = class.
