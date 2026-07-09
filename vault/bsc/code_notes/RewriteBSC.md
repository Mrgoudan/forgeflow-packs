# RewriteBSC.cpp

Source: `clang/lib/Frontend/Rewrite/RewriteBSC.cpp`.

Frontend rewriter that converts a BSC translation unit to plain C source. Activated via `-rewrite-bsc`. F09 (codegen drop on `_Generic`/`__builtin_choose_expr`) has its surface here.

## Functions

### `HandleTranslationUnit` — :310-341
**Invariant**: top-level orchestrator — clears the main file's text, then re-emits include directives, macro directives, decls, all into a buffer that becomes the new C output.
**Sequence**: `CollectIncludes` → `RewriteMacroDirectives` → `FindDeclsWithoutBSCFeature` → `RewriteDecls` → buffer flush.

### `FindDeclsWithoutBSCFeature` — :509-528
**Invariant**: classifies top-level Enum/Function/Record/Var decls into "has BSC features" vs "doesn't". Used downstream to decide pretty-printer vs original-text.
**Switch coverage**: only Enum, Function, Record, Var. **Misses**: TypeAlias, Namespace, StaticAssert, Empty, etc. Those decls fall to default and never enter `DeclsWithoutBSCFeature`. Downstream `find(D) == end()` returns true → forced to pretty-printer path. Typedefs are thus always pretty-printed (no source-text preservation). Probably intentional, not a bug.

### `RewriteNonGenericFuncAndVar` — :888-965
**Invariant**: per-decl emission. Functions with BSC features → pretty-printer (`FD->print(Buf, Policy)`). Others → original source text via `Lexer::getSourceText`.
**F09 surface**: the `FD->print` path emits a rewritten body. For BSC's `_Generic`/`__builtin_choose_expr` containing CallExpr side-effects, the printed body silently drops the side-effect — emits an undefined `_borrowck_tmp_N` identifier.

## Candidates

1. **C3 RewriteNonGenericFuncAndVar pretty-printer drops _Generic side effects** — **CONFIRMED-F09** (IJO88R).
2. **`_Owned struct` family pretty-printing** — out of scope per user.
3. **TypeAliasDecl with BSC features** — PROBED-SOUND (2026-06-17): printer strips _Owned/_Borrow/_Nonnull/_Nullable/_ArrayElem across basic, new-style `typedef A=...`, owned struct-field, and fnptr-ret-owned typedefs; 0 leftover keywords; rewritten output compiles as plain C (round-trip rc=0). probes rw1/rw2.

## Probe candidates ranked

1. **C3 TypeAlias with _Owned via macro expansion** — what if the typedef body contains a macro that expands to _Owned? Pretty-printer might mishandle.

## Not yet read

- `RewriteDecls` (line 635) — main decl-emission loop
- `RewriteInstantFunctionDef` (line 967) — generic instantiation rewriting (out of scope per user)
- `RewriteTypeDefinitions` (line 787)
- `RewriteInstantFunctionDecl` (line 813)
- `RewriteMacroDirectives` (line 596)

## 2026-05-21 Explorer cycle — `BSCFeatureFinder` (WalkerBSC.h) miss on `sizeof`/`_Alignof` type-operand

**Surface**: `clang/include/clang/AST/BSC/WalkerBSC.h:48-363` (`BSCFeatureFinder`), invoked from `RewriteBSC::FindDeclsWithoutBSCFeature` (RewriteBSC.cpp:509-528). Determines whether a top-level FunctionDecl is "BSC-feature-bearing" and thus must be routed through DeclPrinter (which strips BSC qualifiers) vs the `Lexer::getSourceText` verbatim branch at RewriteBSC.cpp:917-925 (which emits the original BSC source unchanged).

**Invariant**: any BSC qualifier (`_Owned`, `_Borrow`, etc.) appearing anywhere syntactically inside a function body must cause `BSCFeatureFinder::VisitFunctionDecl(FD)` to return true.

**Defect** (CONFIRMED-new, distinct from F54, F09, F14):
- The default `VisitStmt(S)` (WalkerBSC.h:210-219) only iterates `S->children()`. For `UnaryExprOrTypeTraitExpr` (i.e. `sizeof(T)` / `_Alignof(T)` with a TYPE operand), `Stmt::children()` returns NO children (the type operand is stored as a `TypeSourceInfo*`, not a Stmt sub-node). No override of `VisitUnaryExprOrTypeTraitExpr` exists.
- Consequence: a function whose ONLY BSC feature is the type operand of a `sizeof`/`_Alignof` expression is classified as "non-BSC" → `DeclsWithoutBSCFeature.insert(FD)` → at RewriteBSC.cpp:911 the source-text verbatim branch fires → output `.c` file contains literal `_Owned` / `_Borrow` tokens → fails to parse as C.

**Probes**:
- Probe 6 `/tmp/explorer_probe.gCXELz.cbs` — `sizeof(int *_Owned)` in fn body → rewriter output `/tmp/probe6_rewritten.c` contains `sizeof(int *_Owned)`; reject as C with "expected ')'" at the `_Owned` token. **CONFIRMED-new**.
- Probe 7 `/tmp/explorer_probe.hUc3Rx.cbs` — `_Alignof(int *_Borrow)` cousin → same failure mode. Confirms `UnaryExprOrTypeTraitExpr` is the surface, not `sizeof` specifically.
- Baseline `/tmp/explorer_baseline.GjlEp8.cbs` — same shape but `_Borrow` in parameter type → VisitFunctionDecl's parameter loop catches it → pretty-printer strips qualifier → output parses clean as C. Asymmetry confirmed.

**Blast radius (hypothesized siblings, unprobed)**:
- `_Owned` / `_Borrow` / `_Nullable` / `_Nonnull` / `_ArrayElem` inside any `sizeof(T)`, `_Alignof(T)`, `__alignof(T)`, `typeof(T)` type-operand position.
- Any BSC qualifier appearing only in a cast-target type that survives the walker (`offsetof(T, .f)` if used).
- A struct typedef inside the function body whose field types carry BSC qualifiers; walker may not recurse into the local typedef's referent record. (Untested.)

**Defect class**: C3 (Visit/switch coverage gap) — applied to `BSCFeatureFinder`'s Expr/Stmt dispatch. Distinct from F37 (Sema UO_Deref check ignoring unevaluated context), F09 (rewriter dropping selected-arm side effects), F54 (rewriter outer-decl-switch missing StaticAssert). New file/function/invariant.

**Fix surface**: add `bool VisitUnaryExprOrTypeTraitExpr(UnaryExprOrTypeTraitExpr *E)` to `BSCFeatureFinder` (WalkerBSC.h around line 285) that checks `E->isArgumentType() ? VisitQualType(E->getArgumentType()) : Visit(E->getArgumentExpr())`.

## SafeExpr / UnsafeExpr rewriting — PROBED-clean (2026-05-29)
`-rewrite-bsc` strips `_Safe`/`_Unsafe` keyword tokens and unwraps the
parenthesized sub-expr: `return _Safe(a + b);` → `return (a + b);`,
`int c = _Unsafe(x * 2);` → `int c = (x * 2);`. Sub-expr preserved (no F09-style
side-effect drop), no literal keyword leaks to .c. No defect. Completes the
cross-layer SafeExpr audit (codegen gaps are F60/F63/F69; rewriter + Sema clean).

## 2026-05-30 Explorer cycle — Chain M in-scope round-trip + SafeFeatureFinder gate

### Rewriter (DeclPrinter) round-trip: in-scope constructs ALL clean
Ran `-rewrite-bsc` on these IN-SCOPE non-generic shapes and compiled the output as plain C
(`clang -fsyntax-only -x c`). All produced well-formed C with BSC qualifiers correctly
stripped, NO leaked `_Owned`/`_Borrow`/`_ArrayElem`/`_borrowck_tmp` tokens, plain-c-exit=0:
- struct-with-`_Owned`-field (param + body) — pretty-printer strips field qualifier.
- `__attribute__((ensure_init))` on owned-pointee param — attribute floats to outer decl,
  `_Owned` stripped; benign (BSC attr ignored by plain C). NOT a bug.
- `typedef int *_Owned T;`, fnptr typedef `void(*)(int *_Owned)`, struct-with-fnptr-field
  `int(*)(int *_Borrow)`, nested-typedef-with-owned-field — all stripped cleanly.
- Prologue-hoisting in-scope constructs through rewriter: `&&`/`||` with non-void call operand,
  nested non-void call `len(mk())`, `?:` with `_Borrow` operands — rewriter prints the
  Epilogue-RESTORED AST (not the `_borrowck_tmp` form), so NO undefined temp leaks (unlike F09's
  `_Generic`/`__builtin_choose_expr` OOS path). Confirms F09 is OOS-construct-specific.
- compound-literal cast-type `(int *_Borrow _ArrayElem){arr}` — declared var catches it, clean.
- local typedef `typedef int *_Owned OT;` sole-feature — VarDecl init type catches it, clean.
- array-of-`_Owned` (`int *_Owned items[3]`, param, local) — SHAPE-REJECTED by Sema
  ("type of array cannot be qualified by '_Owned'").

**Verdict: Chain M in-scope rewriter slice is SOUND for the common non-generic constructs.**
The only live rewriter-routing defect remains F59 (`UnaryExprOrTypeTraitExpr` type-operand walker
gap in `BSCFeatureFinder`); F54 (decl-switch StaticAssert); F09/F38 (OOS `_Generic`/ChooseExpr).

### SafeFeatureFinder borrow/ownership GATE (WalkerBSC.h:367-461) — SOUND-but-smelly negative
`Sema::FindSafeFeatures` (SemaDeclBSC.cpp:203, via `SafeFeatureFinder::FindOwnedOrBorrow`) gates
`RequireBorrowCheck` (SemaDeclBSC.cpp:279) which gates BOTH the CFG **ownership analysis**
(:395) AND the **borrow checker** (:408). If it returns false for a function that actually has
owned/borrow features, both CFG analyses are skipped.

`SafeFeatureFinder::VisitQualType` (WalkerBSC.h:374-380) is WEAKER than `BSCFeatureFinder`'s:
it checks only `isOwnedQualified() || isBorrowQualified() || hasOwnedFields() || hasBorrowFields()`.
- `hasOwnedFields()`/`hasBorrowFields()` (TypeBSC.cpp:57-99) recurse through `PointerType` pointee
  and `RecordType` fields — so `int *_Owned *` / `int *_Borrow *` (nested behind raw pointer) ARE
  caught (pointee `isOwnedQualified`). My nested-pointee hypothesis is WRONG → detected.
- BUT neither recurses into a **FunctionProtoType**'s params/return. So an owned/borrow qualifier
  buried ONLY in a function-pointer signature (`void(*)(int *_Owned)`, `int *_Owned(*)(void)`) is
  MISSED by `hasOwnedFields()` → `FindSafeFeatures` returns false → CFG ownership+borrow SKIPPED.
  (Contrast: `BSCFeatureFinder::VisitType` WalkerBSC.h:73-82 DOES recurse into FunctionProtoType.)

**Why this is currently UNOBSERVABLE (not filed):** a CFG-ownership/borrow violation needs an
OUTER named `_Owned`/`_Borrow` entity (var/param/return) or an `&_Mut`/`&_Const` operator to
exist in the body — all of which `VisitQualType` (outer `isOwnedQualified`) or `VisitUnaryOperator`
(UO_AddrMut/AddrConst) DO catch, flipping the gate true. A fnptr-signature-buried qualifier
creates NO body-level borrow/move obligation by itself; calling the fnptr produces an owned TEMP
whose leak is caught by the SEPARATE Sema-time `CheckTemporaryVarMemoryLeak` (Chain A), which is
gate-independent (verified: `void f(int *_Owned(*mk)(void)){ mk(); }` fires "temporary _Owned leak"
identically with/without a gate-visible dummy). So the gate-miss has no reachable false-negative
on the in-scope surface. Logged as a code smell (cf. the BSCIRBuilder &&/|| Move→Copy smell in
Chain E). **Reopen-if:** a future construct lets a fnptr-buried `_Owned`/`_Borrow` create a
body-level CFG ownership/borrow obligation without any gate-visible outer feature.

## chain-Z comma-wrapped borrow rewrite — PROBED-SOUND 2026-06-04

`int *_Borrow m = (0, &_Mut x)` rewrites to `int * m = (0 , &x);` — valid C, comma preserved, no undefined
borrow-temp (contrast F09). The rewriter is NOT affected by the F91/F92/F93 comma blind spot: it emits
correct output regardless of whether the borrow checker extracted an action. Confirms chain Z's
unsoundness is confined to the dataflow CHECKS, not the source-to-source rewrite.

## ensure_init_if_ret attribute preservation in -rewrite-bsc — probing
**Invariant**: -rewrite-bsc must preserve `ensure_init_if_ret(N)` on params in
output; dropping it silently strips the contract from rewritten code (cf. F54
dropped top-level _Static_assert).
**Candidates**: 1. rewrite a fn decl + def with ensure_init_if_ret → attribute in output?

## ensure_init_if_ret attribute not stripped by -rewrite-bsc (LOW, 2026-06-08)
INVARIANT: -rewrite-bsc should emit portable C — BSC-specific keywords/attributes stripped.
OBSERVED: strips `_Safe`/`_Borrow`/`_Owned` correctly, but PRESERVES
`__attribute__((ensure_init_if_ret(0)))` in output (line "int init_it( __attribute__((ensure_init_if_ret(0))) int * out)").
gcc → "'ensure_init_if_ret' attribute directive ignored [-Wattributes]"; fails under -Werror.
SEVERITY: LOW — functionally harmless (attribute is a compile-time contract, ignoring it at
C-compile is correct; no semantic change, unlike F54 which dropped _Static_assert). Impact is
cosmetic (-Wattributes warning) / -Werror-build-only. The rewriter wasn't updated to strip the
new ensure_init_if_ret attribute. CONFIRMED same for `ensure_init` (also leaks into output). NOT FILED (LOW per policy).
Fix: add ensure_init_if_ret/ensure_init to the attribute-stripping pass in RewriteBSC.

## _Nullable/_Nonnull not stripped → not gcc-portable = F116 (FILED 2026-06-25). Rewriter strips _Owned/_Borrow/_Safe but keeps the nullability quals; gcc errors, clang ok. Violates manual gcc-compat promise (6669). Fix: strip in type-spelling path.

## RewriteBSC::RewriteRecordDeclaration (RewriteBSC.cpp:707) — struct forward-decl emission
- **Invariant**: emits a portable C forward decl `struct S;` per record (sets incomplete, RD->print(Policy), restores); the body + field-qualifier stripping is via the PrintingPolicy elsewhere.
- **Peers**: RewriteTypeDefinitions (body), RewriteNonGenericFuncAndVar (F54), F09 (borrowck_tmp undefined), F116 (nullable prototype breaks plain compiler).
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: struct `{int*_Owned _Nullable f;}` + `_Safe void g(int*_Borrow p){ int*_Borrow q=&_Mut *p; *q=5; }` → -rewrite-bsc emits 0 leaked BSC qualifiers, 0 borrowck_tmp; `&_Mut *p`→`&*p`, `int*_Borrow q`→`int * q`; output compiles as plain C (see rc). Rewriter strips qualifiers portably for this construct. (2) borrowck_tmp temp hoisting for a borrow op (F09 class). (3) forward-decl ordering vs use.
