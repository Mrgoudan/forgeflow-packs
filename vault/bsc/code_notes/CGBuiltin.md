# clang/lib/CodeGen/CGBuiltin.cpp — BSC additions

Only one BSC-specific builtin handler exists at CGBuiltin.cpp:2795-2799:

```cpp
#if ENABLE_BSC
case Builtin::BI__assume_initialized:
  // Semantic-only builtin for init analysis; no-op at runtime.
  return RValue::get(nullptr);
#endif
```

## EmitBuiltinExpr / BI__assume_initialized handler (CGBuiltin.cpp:2795-2799)

**Invariant**: A no-op builtin must still evaluate its argument expression if
the argument can have side effects, OR must declare itself a sema-only construct
that REJECTS arguments with side effects.

**Peers** (other "no-op-at-codegen" builtin handlers):
- `Builtin::BI__assume` / `BI__builtin_assume` (CGBuiltin.cpp:2800-2807): explicitly
  checks `E->getArg(0)->HasSideEffects(getContext())` and ALSO emits `Intrinsic::assume`
  to record the assumption. The side-effect check is DEFENSIVE — if HasSideEffects, it
  returns nullptr (so the value is dropped, but the side-effect-check is the contract).
- `Builtin::BI__builtin_expect` etc.: always evaluate the arg.

**Candidates**:
1. **`__assume_initialized` argument is **NEVER** evaluated at codegen** — PROBED-confirmed
   (this session). `RValue::get(nullptr)` is returned immediately with no `EmitIgnoredExpr`
   or `EmitScalarExpr` on `E->getArg(0)`. Sema's check (`SemaChecking.cpp:2295-2330`) only
   verifies the arg is a `&...`-form and walks via MemberExpr/UO_Deref bases — it does
   NOT reject CallExpr-containing arguments. So forms like `__assume_initialized(&foo()->x)`
   pass Sema, and the entire CallExpr is silently dropped at codegen.

2. Unprobed: would `BI__assume`'s `HasSideEffects` check actually preserve the call?
   It returns `RValue::get(nullptr)` WITHOUT evaluating, so it has the same drop —
   but the Sema-level checker for `__builtin_assume` already warns on side-effect args,
   so the surface is closed at Sema. `__assume_initialized` has no such Sema warning.

## BSC ownership-transfer builtins (CGBuiltin.cpp:5369-5382) — READ 2026-05-30

**Handler** for `__move_to_raw` / `__take_from_raw` / `__move_array_to_raw` /
`__take_array_from_raw`. Treats them as no-op casts:
```cpp
assert(E->getNumArgs() == 1);
Value *ArgVal = EmitScalarExpr(E->getArg(0));   // arg IS evaluated (once)
llvm::Type *RetTy = ConvertType(E->getType());
if (ArgVal->getType() != RetTy) ArgVal = Builder.CreatePointerCast(ArgVal, RetTy);
return RValue::get(ArgVal);
```
**Invariant**: the single argument's side effects must be emitted exactly once;
the value is reinterpreted (pointer-cast) not copied. **Reasoned-safe**: unlike
`__assume_initialized` (F58), this path DOES `EmitScalarExpr(arg)` so no drop. Uses
`EmitScalarExpr` — correct because all four builtins take a SCALAR `_Owned`/`*` arg
(an `_Owned` pointer or raw pointer), never an aggregate. No double-emit (single
`ArgVal`). **No defect found.**

## CodeGen owned-DROP emission — call-chain audit (Mode 2) 2026-05-30 — NEGATIVE

**Hunt**: does codegen emit the `_Owned` free (`safe_free` call) the WRONG number of
times vs what the analyzer accepted → runtime leak (missed free) or double-free
(extra free) on a CHECKER-ACCEPTED program? Oracle: `scripts/vg_probe.sh`.

**Key structural finding**: BSC codegen synthesizes NO implicit owned-drop. There is
no CGDeclBSC.cpp / CGExprBSC.cpp; no EHScope/cleanup registration for `_Owned` locals;
no `__destruct`/Drop emission. `grep getLangOpts().BSC clang/lib/CodeGen` → only
`hasTraitType()` paths (OOS). Every owned free is an EXPLICIT `safe_free` CALL the
user wrote; codegen lowers it via the ordinary C call path (emitted once). An
aggregate owned-struct copy/move is a standard `EmitAggregateCopy` / `callCStruct*`
memcpy (CGExprAgg.cpp:341-361) — bitwise, no per-field free. The analyzer guarantees
the source is never re-freed; codegen needs no source-nulling. **Therefore a codegen
wrong-free-count requires either (a) codegen to DROP/DUPLICATE an explicit call expr
(that is the F58 side-effect-drop class, now Sema-gated), or (b) an ANALYZER FN that
accepts a re-consume (F64/F67/F75) paired with a codegen no-op. There is no
codegen-ONLY owned-drop divergence on the reachable in-scope surface.**

Probed (all CHECKER-ACCEPTED + valgrind CLEAN unless noted):
- owned consume baseline; owned `?:`-into-var then consume; owned consume-then-`break`
  in loop; owned moved into struct field then struct consumed; owned struct returned
  by value (return-slot move) then consumed; two-return-path owned struct (free the
  unreturned arm); owned consumed on both branches with forward `goto` convergence;
  SafeExpr-wrapped aggregate owned-struct by-value arg. → all 1-alloc/1-free, ERROR
  SUMMARY 0, 0 lost.
- owned `?:` with consume in BOTH arms (`c ? consume(a) : consume(b)`) → SHAPE-REJECTED
  ("use of moved value: b" — analyzer conservative on `?:`-arm consume).
- SafeExpr-wrapped owned consume inside a `_Safe` fn body → SHAPE-REJECTED
  ("_Unsafe function call is forbidden in the safe zone").

SafeExpr codegen emitter coverage (F60/F63/F69 family): VisitSafeExpr present in
scalar (CGExprScalar.cpp:452), complex (CGExprComplex.cpp:116), aggregate
(CGExprAgg.cpp:128), lvalue (CGExpr.cpp:1368). MISSING in constant emitter
(CGExprConstant.cpp) → that is F69 (still open, already filed). Not new.

## Filed candidates

- Candidate 1 → PROBED-confirmed-new (this session): see handoff payload.
  - Minimal repro: `/tmp/explorer_probe.Y8nwPj.cbs`
  - Baseline (asymmetry): `/tmp/explorer_probe.rNBYmv.cbs`
  - Symptom: silent side-effect drop. Compose with init-analysis: can produce
    runtime uninitialized read (valgrind catches it) when user expects the
    side-effecting call inside the address-of arg to initialize the memory.
