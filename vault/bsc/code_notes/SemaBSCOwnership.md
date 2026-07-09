# SemaBSCOwnership.cpp

Source: `clang/lib/Sema/BSC/SemaBSCOwnership.cpp`.

Sema-time ownership checks. Runs at parse-time, before the CFG dataflow in `Analysis/BSC/BSCOwnership.cpp`. Catches statement-level patterns that are *guaranteed* leaks regardless of control flow (e.g. unused expression-stmt of an _Owned-returning call).

## CheckOwnedQualTypeCStyleCast (SemaBSCOwnership.cpp:273-327) — read 2026-06-17, PROBED-SOUND

**Invariant**: an explicit C-style cast involving an `_Owned` type must not LAUNDER
ownership — owned↔borrow (:300) and owned↔raw (:304) are forbidden (prevents
fabricating/escaping ownership → double-free/leak); _ArrayElem mismatch forbidden
(:307); raw↔raw allowed (:310); void+owned both directions allowed (:316, type-erase
for free() + re-type); owned→integer allowed but NOT reverse (:322); dependent RHS
DEFERs (:288). The cast itself is MOVE-tracked by the ownership dataflow (the operand
is consumed), so void+owned re-typing cannot create an aliasing duplicate.

**Peers**: CheckBorrowQualTypeCStyleCast (:605, borrow sibling); IsSafeConversion
(safe-zone gate, runs first); the ownership TransferFunctions move-tracking
(BSCOwnership.cpp) — the gate decides type legality, the dataflow enforces the move.

**Candidates (all closed by reading + probe)**:
1. (composition) **void+owned re-typing aliasing → double-free** — PROBED-SOUND: o1
   `*p` after `(void*_Owned)p` → "use of moved value: p"; o2 unfreed `v` → "memory leak
   of value: v"; o3 move+free clean. The cast moves the source; dest owns; no duplicate,
   no silent leak. Re-validated on rebuilt compiler 2026-06-17.
2. (reachability) **dependent check is RHS-only (:288)** vs borrow's RHS||LHS — BENIGN:
   CheckCStyleCast (SemaCast:2875) defers on DEST(LHS)-dependent BEFORE calling this, so
   dependent-LHS never reaches here; only dependent-RHS does, which :288 handles. SOUND.
3. (symmetry) owned→int (:322) / owned-diff-pointee — already PROBED-rejected in safe zone
   (_probed.md :148-149). Closed.

## CheckBorrowQualTypeCStyleCast (SemaBSCOwnership.cpp:605-666) — read 2026-06-17

**Invariant**: an explicit C-style cast involving a `_Borrow` type is allowed only
if it preserves borrow compatibility — same type; nullptr→borrow; void* widening;
DROP `_ArrayElem` w/ matching pointee; NOT casting away pointee const
(`isCastingAwayConst`, :663); and NOT crossing mut↔const borrow (:642, "prevent
casting between mutable and const borrows to avoid aliasing"). Dependent types
DEFER to instantiation (:617). Recurses into pointees (:665).

**Peers**: IsSafeConversion→IsSafePointerConversion (runs FIRST at SemaCast:2911 in
safe zone; rejects drop-const via IsDroppingConst — so the mut/const violation is
double-gated in the safe zone). CheckBorrowQualTypeAssignment (:677, assignment peer).
CheckOwnedQualTypeCStyleCast (:273, owned sibling).

**Candidates (ranked)**:
1. (symmetry) **mut/const check (:642) runs AFTER void-pointer allow (:635)** — a cast
   to ANY `void*` (line 635, doesn't even require borrow on LHS) returns true before the
   mut/const aliasing check. So `void*_Borrow ← const int*_Borrow` skips :642; BACKSTOP
   must be IsSafeConversion (drop-const). Probe const→mut borrow cast directly + via void
   detour, in the safe zone. TOP.
2. (composition) **void detour round-trip** `const int*_Borrow → void*_Borrow →
   int*_Borrow` to launder const→mut. Hop-2 (void→typed) needs `!IsInSafeZone()` (:637)
   so blocked in safe zone; hop-1 needs drop-const past IsSafeConversion. Probe.
3. (reachability) **integer ← borrow (:629)** allowed unconditionally — but safe-zone
   IsSafeConversion rejects ptr→int first; unsafe-zone only. Low.

## CheckBorrowQualTypeAssignment 3-arg (SemaBSCOwnership.cpp:677-711) — read 2026-06-17 [CHAIN: assignment peer of the cast gate]

**Invariant**: assignment/init of a `_Borrow`-typed LHS from a `_Borrow` RHS is
allowed only if borrow-ness matches (:684, else false), _ArrayElem only DROPPED
(:685-691), trait-impl/same-type/void-with-matching-pointee-const (:699-700), else
recurse pointee (:706).

**Peers (CHAIN)**: CheckBorrowQualTypeCStyleCast (:605, the cast peer) +
IsSafeConversion→IsSafePointerConversion (the safe-zone conversion gate). All three
must AGREE on borrow conversions or the observable result depends on call order.

**Diff vs the cast gate (:605)** — two asymmetries found by reading:
- (a) void* LHS: cast :635 allows UNCONDITIONALLY; assignment :699 requires pointee
  const match. (Cast backstopped by IsSafeConversion → no net gap, confirmed b2.)
- (b) **adding const**: assignment recursion :706 compares pointees by `hasSameType`,
  so `const int*_Borrow ← int*_Borrow` (add const) recurses `hasSameType(const int,int)`
  → false → REJECT. But IsSafePointerConversion ALLOWS add-const, and docs/repro say it's
  allowed. So either (i) the 3-arg fn isn't on the init/assign path, or (ii) const-add is
  actually rejected by this gate but the OTHER gate runs first/instead. PROBE which.

**Candidates (ranked)**:
1. (symmetry/CHAIN) **const-add disagreement** — PROBED-FOLDED-F27 (IJOKAC, MEDIUM): add-const
   allowed via init+assign, REJECTED via explicit cast (`incompatible _Borrow types`). The cast
   gate :642 symmetric `isConstBorrow()` mismatch rejects the SAFE mut→const dir; IsSafePointer-
   Conversion allows add-const. Re-validated reproducing on current rebuilt compiler 2026-06-17.

## Functions

### `CheckTemporaryVarMemoryLeak` — :534+
**Invariant**: an expression statement whose top-level expression is a CallExpr returning `_Owned` (or a struct with owned fields, or via typedef of the same) must bind the result somewhere — otherwise the temporary leaks.
**Wrapper handling**: uses **`dyn_cast<CallExpr>(E)`** — does **not** strip parens or implicit casts.
**Exemplar**: **F14** — `(void)f(p);`, `(f(p));`, `(0, f(p));` all bypass this check because the top-level expr is CStyleCastExpr / ParenExpr / BinaryOperator(comma) — not a CallExpr.

### `CheckMoveVarMemoryLeak` — :547+
**Invariant**: analogous check for move-class expressions.
**Wrapper handling**: uses **`IgnoreParenCasts()`** before classification.
**Asymmetric peer to `CheckTemporaryVarMemoryLeak`**: the two are written by different hands or at different times; the strip-style disagreement IS the C1 defect.

## Candidate status (ranked, with progress)

1. **C1 — CONFIRMED-F14** (filed). Variants seen and folded: paren / cast / comma / cond / _Generic / __builtin_choose_expr. **Do not probe further variants of this site** — one fix at line 535 (add `IgnoreParenCasts` + recursive walker) resolves all.
2. **C4 — CONFIRMED-F14 surface** with `CheckMoveVarMemoryLeak`. Already at `IgnoreParenCasts` — the asymmetric peer is the WORSE side (raw dyn_cast). One fix mirrors the right side. Folded into F14.
2. **C4 — check pair asymmetry** — there should be one canonical "is this expression a leaking-temp-producer" predicate, used by both `CheckTemporaryVarMemoryLeak` and `CheckMoveVarMemoryLeak`. Right now they're parallel. Audit: are there shapes one catches that the other doesn't (false negative class)?

## Not yet read

- The full set of leak-check entrypoints (where does Sema call CheckTemporaryVarMemoryLeak from?)
- Are there other "owned-shape predicate" sites that suffer the same C1 issue?

## Newly identified bugs (this session)

### F20 — CompoundLiteralExpr at expr-stmt level bypasses temp-leak check
- Site: `CheckTemporaryVarMemoryLeak` at `:534` only `dyn_cast<CallExpr>(E)`. CompoundLiteralExpr never recognized as leak-producing temp.
- Fix: extend predicate to also accept `dyn_cast<CompoundLiteralExpr>(Stripped)` if its type is owned-qualified or has owned fields.
- Filed: **F20 (IJOH96)**.

### F21 — CheckMoveVarMemoryLeak inspects only immediate base's type
- Site: `:555-558` checks `ME->getBase()->getType().isBorrowQualified()`. For `(*s).f` (where s is _Borrow), the base is `UnaryOperator(Deref, s)` whose type is the dereferenced struct (NOT borrow). Check returns false → move-through-borrow bypass.
- Same defect for chained member access `s_borrow->t.g` — `getBase()` is `s_borrow->t` whose type is struct T (not borrow).
- Fix: walk the base through `UO_Deref` / `UO_AddrConstDeref` / `UO_AddrMutDeref` and through `MemberExpr` chains to find the original borrow ancestor.
- Filed: **F21 (IJOHFG)**.

## CheckMoveVarMemoryLeak call sites (all affected by F21)
1. SemaStmt.cpp:3937 — return-stmt operand → confirmed bypass for `return (*s).f`
2. SemaExpr.cpp:6728 — call-arg → confirmed bypass for `consume((*s).f)`
3. ParseExpr.cpp:646 — assignment RHS in parser → likely same bypass
4. DeclStmt init path (Sema's init-list handler) → confirmed bypass via F21 repro

## CheckTemporaryVarMemoryLeak (SemaBSCOwnership.cpp:534-545) — PROBED-confirmed-F47 (HIGH; co-located with F14 IJOAO8)

**Invariant**: given an expression that produces a temporary value, diagnose if the temp's type carries unfreed `_Owned` resources.

**Peers**: invoked from SemaExprMember.cpp:1288 (member access base), SemaStmt.cpp:55 (expression statement), SemaExpr.cpp:16557-58 (LHS/RHS of assignment), SemaExpr.cpp:17120 (UO_Deref operand). The body's TYPE predicate (`isOwnedQualified() || isMoveSemanticType()`) recognizes both pure-`_Owned` types AND structs-with-`_Owned`-fields.

**Filed candidates (multiple)**:
- F14 (IJOAO8): wrapper-list around CallExpr is single-shape (`dyn_cast<CallExpr>(E)` doesn't strip paren/cast/comma/cond wrappers).
- F47 (IJOSRL): the allowed-Expr-kind set itself is too narrow — only CallExpr; CompoundLiteralExpr (also a C99 temp-source) bypasses.

**Soundness consequence (F47)**: `(struct B){.p = mk()}.p == nullptr` compiles clean; valgrind reports leak. C99 idiom recommended in BSC docs — straightforward unsafe code accepted by the compiler.

**Sibling shapes folded into F47**:
- bare stmt `(struct B){.p = mk()}.p;`
- function arg `f((struct B){.p = mk()}.p)`
- both leak same way.

**Fix surface**: combine F14's wrapper-stripping with F47's expanded recognized-temp set:
```cpp
Expr *S = E->IgnoreParenCasts();
if (!isa<CallExpr>(S) && !isa<CompoundLiteralExpr>(S)) return false;
```

## CheckMoveVarMemoryLeak (SemaBSCOwnership.cpp:547-559) — PROBED-confirmed-NEW 2026-05-21 (HIGH, SafeExpr/_Unsafe(...) wrapper bypass)

**Invariant**: `IgnoreParenCasts()` is supposed to peel all the trivial pass-through wrappers (paren, cast, GenericSelectionExpr, ChooseExpr, MaterializeTemporaryExpr, FullExpr, UO_Extension) before the dispatch tests `isa<UnaryOperator>` / `isa<MemberExpr>`. The two dispatch arms then catch move-through-borrow patterns.

**Hole**: `IgnoreParenCasts()` strips standard clang wrappers — it does NOT strip BSC-specific `SafeExpr` (defined in `clang/include/clang/AST/BSC/ExprBSC.h:59`). `SafeExpr` is the AST node parsed from `_Safe(expr)` and `_Unsafe(expr)` (`Parser::ParseSafeExpression` at `clang/lib/Parse/BSC/ParseExprBSC.cpp:206-237`). It is a single-child wrapper just like ParenExpr.

**Consequence**: `int *_Owned q = _Unsafe(s->f);` (where `s` is `_Borrow` to struct with `_Owned` field) compiles clean. Bare `int *_Owned q = s->f;` correctly emits `err_move_borrow`.

**Filed**: NEW (to be filed by main thread).

**Distinct from F21** (paren-deref-of-borrow `(*s).f` — bug inside the MemberExpr arm's `getBase()->getType()` check). F21 wraps the *base* of a MemberExpr; the SafeExpr bypass wraps the *entire move expression* and prevents the dispatch from finding any MemberExpr at all.

**Distinct from F25** (ConditionalOperator wrapper). F25's fold list explicitly enumerates `BO_Comma`, `BinaryConditionalOperator`, `_Generic`/`__builtin_choose_expr` selected arm, `StmtExpr` — these are all expression kinds with multiple children where the proposed fix is "recurse into arms." SafeExpr is a single-child pass-through; the fix surface is `IgnoreSafeExprSingleStep` (cross-cutting in `IgnoreExpr.h`) or per-site `if (auto SE = dyn_cast<SafeExpr>(E)) E = SE->getSubExpr();` — different code surface from F25's per-call-site arm-recursion.

**Sibling sites for blast radius**: every other Sema/Ownership check that uses `IgnoreParenCasts()` on a user-controlled expression position can be similarly bypassed by `_Unsafe(...)`/`_Safe(...)` wrapping. E.g. `CheckTemporaryVarMemoryLeak` at `:534` uses raw `dyn_cast<CallExpr>(E)` (no Ignore-strip — F14 wider problem), `getMemberFullField` (F30), `BuildUnaryOp`'s UO_AddrMut string-literal guard (F36 area), `CheckBorrowQualTypeCStyleCast`, etc. The `SafeExpr` strip omission is a clang-AST-level gap that potentially affects many BSC checks.

**Repro**: `/tmp/explorer_probe.88hd4X.cbs`. Baseline: `/tmp/explorer_probe.SKxgMS.cbs`.

## CheckOwnedOrIndirectOwnedType (SemaBSCOwnership.cpp:110-126) — UNPROBED

**Invariant**: for global variables, union fields, and arrays, reject T if T directly carries `_Owned` (top-level qualifier) OR T is "move-semantic" (Type::isMoveSemanticType — `_Owned`-qualified itself, OR a struct with at least one `_Owned`-qualified field, transitively).

**Peers**:
- `CheckBorrowOrIndirectBorrowType` (line 966-981) — analogous borrow side; uses `T->hasBorrowFields()` for "indirect" detection.

**Asymmetry**:
- `RecordType::hasBorrowFields` (TypeBSC.cpp:449-481) walks the **pointer-pointee chain** for every field, so `struct S { int *_Borrow * f; }` (field is pointer-to-borrow-pointer, outer is not borrow) correctly returns true.
- `isMoveSemanticTypeImpl` (TypeBSC.cpp:334-361) for record fields **only checks the field's top-level `isOwnedQualified()`** and then recurses into RecordType-typed fields. It does NOT walk through pointer-pointee chains. So `struct S { int *_Owned * f; }` returns false — the field is a pointer (not `_Owned`-qualified), and not a record, so the loop body does nothing.
- The class comment at TypeBSC.cpp:328-332 says explicitly: "`struct S6 { int *owned * p }` is NOT move semantic" — by design, but `CheckOwnedOrIndirectOwnedType` is precisely the gate that's supposed to reject this AT GLOBAL/UNION/ARRAY scope where ownership can't be transferred. The asymmetry leaves a hole.

**Candidates**:
1. **Global struct of type `struct S { int *_Owned * p; }`**: `CheckOwnedOrIndirectOwnedType` skips. Compiles silently, leaks at program exit. **TOP CANDIDATE** — distinct fix surface from F41 (different function, different predicate `isMoveSemanticType` vs the inline `isOwnedQualified()` check).
2. Union field of same type — Sema's `union field` env path (SemaDecl.cpp:18574). Same predicate, same gap.
3. Array of structs containing nested owned pointer — `HasInvalidArrayElemPointee` (line 26-55) uses an explicit recursive walk; check whether it overlaps with the `_ArrayElem` requirement but doesn't catch the bare array `struct S arr[10];`.

**Distinct from F41**: F41 is in `CheckOwnedFunctionPointerType` (line 440-479) at fnptr-assignment time; the bug is in the function-prototype variance check. This candidate is in `CheckOwnedOrIndirectOwnedType` at global/union/array decl-time; the bug is in `Type::isMoveSemanticType` not walking pointer-pointee. Different call sites, different predicate.

### CONFIRMED-NEW 2026-05-30 (bsc-explorer, Chain O) — owned/borrow ASYMMETRY at the indirect global/union/array decl gate

**Root cause**: `CheckOwnedOrIndirectOwnedType` (SemaBSCOwnership.cpp:122) gates the
indirect-owned case on `isMoveSemanticType()`, whose impl (`isMoveSemanticTypeImpl`,
TypeBSC.cpp:341-367) tests each field's `isOwnedQualified()` and recurses ONLY into
RecordType-typed fields — it does **NOT** walk a field's pointer-pointee chain. By
contrast the BORROW peer `CheckBorrowOrIndirectBorrowType` (:980) uses
`hasBorrowFields()` (TypeBSC.cpp:456-488) which DOES walk the pointee chain (:471-478).
So for the structurally-identical pair:
- `struct B { int *_Borrow * f; }` as a global/union-field → REJECTED ("contains _Borrow even indirectly", `hasBorrowFields` walks the inner `_Borrow`).
- `struct O { int *_Owned * f; }` as a global/union-field → **ACCEPTED** (`isMoveSemanticType` returns false; the inner `_Owned` is one plain-pointer level deep and never visited).

The owned-pointer-walking peer `RecordType::hasOwnedFields()` (TypeBSC.cpp:423-454)
EXISTS and would catch it — but the global/union/array gate calls
`isMoveSemanticType()`, not `hasOwnedFields()`. Wrong predicate chosen for the owned
side; the borrow side picked the recursing one.

**Asymmetry baseline**: `struct B { int *_Borrow * f; } gb;` REJECTED; identical
`struct O { int *_Owned * f; } go;` ACCEPTED.

**Observable**: a file-scope global owning heap through a buried `int *_Owned *`
compiles clean in `_Safe` code and never frees → leak (the very class
`CheckOwnedOrIndirectOwnedType` exists to prevent: global scope has no owner to run
the drop). Valgrind: definitely-lost.

**Distinct from**: F77 (`AreOwnedBorrowQualifiersCompatible` hetero-redecl outer-only —
that's the redecl-merge path), F79 (`CheckMoveVarMemoryLeak` `isOwnedQualified()`-only —
move-out-through-borrow gate), F57 (no FunctionProtoType recursion), F41/F76 (fnptr
variance assignment). This is the GLOBAL/UNION/ARRAY decl-time indirect-owned gate
(`CheckOwnedOrIndirectOwnedType`), root predicate `isMoveSemanticType` not walking
pointer-pointee while its BORROW peer's `hasBorrowFields` does. Defect class **C1**
(owned/borrow sibling-predicate asymmetry — wrong recursion-depth predicate selected
for the owned dimension).

**Fix surface**: change `CheckOwnedOrIndirectOwnedType`'s indirect arm from
`isMoveSemanticType()` to `hasOwnedFields()` (mirroring the borrow peer), OR make
`isMoveSemanticTypeImpl` walk the pointer-pointee chain like `hasOwnedFields` does.

## Chain V — Borrow-side nested-detection mirror (CheckBorrowOrIndirectBorrowType / CheckNestedBorrowType / hasBorrowFields) — 2026-05-30 TRACING

### Site-symmetry table (who calls the owned vs borrow indirect-type gate)
| decl site | Owned gate | Borrow gate |
|-----------|-----------|-------------|
| global variable (SemaDecl.cpp:8527-8528) | CheckOwnedOrIndirectOwnedType ✓ | CheckBorrowOrIndirectBorrowType ✓ |
| union field (SemaDecl.cpp:18687-18688) | CheckOwnedOrIndirectOwnedType ✓ | CheckBorrowOrIndirectBorrowType ✓ |
| **array element** (SemaType.cpp:5202) | CheckOwnedOrIndirectOwnedType ✓ | **MISSING — no CheckBorrowOrIndirectBorrowType call** |
| BuildPointerType nested-borrow (SemaType.cpp:2120) | (none) | CheckNestedBorrowType (borrow-only; no owned twin) |

**TOP CANDIDATE (site-asymmetry, distinct from F80's predicate-asymmetry):**
At the array declarator (SemaType.cpp:5199-5203) only the OWNED gate runs. So
`struct B { int *_Borrow f; } barr[3];` (array whose element indirectly carries
`_Borrow`) is ACCEPTED while the owned twin `struct O { int *_Owned f; } oarr[3];`
is REJECTED ("type of array cannot be qualified by '_Owned'(even indirectly)").
Confirmed at orient (/tmp/cv_orient.cbs): oarr REJECTED, barr CLEAN.

**Distinct from F80**: F80 is `CheckOwnedOrIndirectOwnedType`'s *predicate*
(`isMoveSemanticType` shallow) being missed on the OWNED side at global/union/array.
THIS candidate is the BORROW gate being entirely ABSENT at the array site — a missing
CALL, not a shallow predicate. The borrow gate itself (`hasBorrowFields`) is the
recursing one. So F80 and this are mirror-opposite gaps (owned: wrong predicate
present; borrow: right predicate but not wired at the array site).

### CONFIRMED-NEW 2026-05-30 (bsc-explorer, Chain V) — pending Fxx
**Root cause**: `SemaType.cpp:5199-5203` (array declarator BSC hook) calls
`CheckOwnedOrIndirectOwnedType(..., "array")` but does NOT call the borrow twin
`CheckBorrowOrIndirectBorrowType`. The two other decl sites (global var
SemaDecl.cpp:8527-8528; union field SemaDecl.cpp:18687-18688) call BOTH gates.
**Observable**: `struct B { int *_Borrow f; } barr[3];` at file scope is ACCEPTED;
the structurally identical owned form `struct O { int *_Owned f; } oarr[3];` is
REJECTED ("type of array cannot be qualified by '_Owned'(even indirectly)"). The
borrow gate is sound (`hasBorrowFields` recurses) and rejects the SAME struct at the
global-var and union-field sites — proving intent; only the array site skips it.
**Runtime**: stash `&_Mut *o` into `garr[0].f`, free the referent, read it back →
valgrind "Invalid read of size 4" (UAF / dangling borrow at a no-lifetime global slot).
**Distinct from F80/F77/F79/F57/F41**: this is a MISSING CALL-SITE (a localized check
skipped at one of three parallel decl sites), not a shallow predicate or a
fnptr/redecl variance hole. Defect class **C6** (localized check skipped on a
parallel site) / C1 (owned/borrow site-asymmetry).
**Fix surface**: add `CheckBorrowOrIndirectBorrowType(D.getIdentifierLoc(), T, "array");`
next to SemaType.cpp:5202.
**Repro**: /tmp/explorer_cv_uaf_FINAL.cbs. Baseline: /tmp/explorer_cv_baseline_FINAL.cbs.

### CheckBorrowFunctionType (:832-855) — body inspected
**Invariant**: a function returning a `_Borrow` (or borrow-containing) type must have
at least one borrow-containing param (lifetime source). Uses `hasBorrow()` on ret+params.
Sound for purpose (no recursion needed — `hasBorrow` is canonical-aware).

## CheckOwnedQualTypeCStyleCast (SemaBSCOwnership.cpp:273-345) — 2026-05-29
**Invariant**: explicit C-style cast between owned-qualified pointers allowed only:
nullptr→owned; raw↔raw; owned→int (unsafe); via void* either side; or recursively
matching pointees. Rejects owned↔borrow, owned↔raw, _ArrayElem mismatch.
**Peers**: F27 (CheckBorrowQualTypeCStyleCast const-mismatch — borrow version);
F41 (CheckOwnedFunctionPointerType only-outer). This RECURSES (:318) unlike F41.
**Candidates**:
1. Line 307 `isArrayElemQualified` on SUGARED LHSType/RHSType (not canonical) — typedef-hidden
   _ArrayElem mismatch? (isArrayElemQualified is likely canonical-aware → benign.)
2. Raw-outer escape (:310) `int *_Owned * → char *_Owned *` allowed (raw outer = C cast); inner
   owned pointee mismatch not checked — but behind raw ptr (unsafe). Likely intentional.
3. void-escape (:316) allows void*_Owned↔T*_Owned both ways at type level; safe-zone rejection
   is separate (IsSafeConversion).
**Probe outcome (2026-05-29): PROBED-SOUND.** `(float *_Owned)int_owned` REJECTED (recursion rejects mismatched pointees); `(void *_Owned)int_owned` cast ALLOWED (void escape) with correct subsequent leak diag; owned↔borrow REJECTED. Recursion handles nested levels (unlike F41 only-outer). No bug.

## CheckEnsureInitFunctionPointerType (SemaBSCOwnership.cpp:879-918) — 2026-05-29
**Invariant**: fnptr assignment — if TARGET param has ensure_init but SOURCE doesn't → error
(contract violation: caller may pass uninit expecting callee-init, source doesn't init). Reverse
(source ensure_init, target not) ok. **Loops OUTER params only (:904), no nested-fnptr recursion.**
**Peers**: F41/F53/F56/F57 (only-outer fnptr variance for owned/borrow/nullability) — ensure_init
dim NOT covered by those. CheckBorrowFunctionType (:854, return-borrow-needs-borrow-param) sound.
**Candidates**:
1. **Nested fnptr ensure_init variance** — `void(*)(void(*)(int* ensure_init))` assigned to
   `void(*)(void(*)(int*))` differs only in INNER param ensure_init → outer loop misses it. Like F41,
   ensure_init dim. BUT (IsSafeFnPtr lesson) must check if silent vs warned. UNPROBED → probing.

**F73 (MEDIUM, 2026-05-29)**: CheckEnsureInitFunctionPointerType (:879-918) only-outer-level — nested-fnptr ensure_init variance SILENTLY accepted (outer-level errors); runtime use-of-uninit (valgrind). C1 family, ensure_init dimension (distinct from F41 owned / F29 null / F53 borrow). Fully silent (unlike IsSafeFnPtr nested _Safe which warns). repro/F73_nested_fnptr_ensure_init_variance.cbs.

## CheckBSCFunctionPointerType DISPATCH GATE (SemaExpr.cpp:10329 + SemaBSCOwnership.cpp:511) — 2026-05-29 Chain-D — CONFIRMED-NEW (pending file)

**Invariant**: at assignment time, when LHS/RHS are function-pointer types whose params/return
differ in a BSC qualifier dimension (owned/borrow/nullability/ensure_init), the fnptr-variance
checks must run.

**Dispatch gate** (CheckAssignmentConstraints, SemaExpr.cpp:10329): `CheckBSCFunctionPointerType`
is invoked ONLY when `OrigLHSType->isFunctionPointerType()`. `Type::isFunctionPointerType()`
(Type.h:7332) returns true iff the IMMEDIATE pointee is a function type. So a
**pointer-TO-function-pointer** (`void(**)(int *_Owned)`, i.e. `PointerType(PointerType(FunctionProtoType))`)
is NOT a function-pointer type → the fnptr dispatch is SKIPPED ENTIRELY.

**Other checks don't cover it either**:
- `CheckBSCQualTypeAssignment` (SemaBSCOwnership.cpp:482) computes `MayHaveOwned`/`MayHaveBorrow`
  from outer `isOwnedQualified()`/`isBorrowQualified()` (false — raw outer levels) and from
  `LHSPtr->hasOwnedFields()`/`hasBorrowFields()` — but those fire only for RECORD-typed pointees,
  NOT function-typed pointees. So `MayHaveOwned`=false → `CheckOwnedQualTypeAssignment` NOT called.
- `CheckNullabilityQualTypeAssignment` (SemaDeclBSC.cpp:156) recurses through `isPointerType()`
  pointees (line 180) but stops when the pointee is a FunctionProtoType.

**Candidates**:
1. **TOP — pointer-to-fnptr owned-param variance.** `void(**)(int *_Owned)` (callee frees param)
   assigned from a function-pointer-typed lvalue whose fnptr param is raw `int *` (or vice versa).
   The fnptr dispatch never fires (outer is ptr-to-ptr, not fnptr). Silent accept → callee
   safe_free()s a non-owned pointer → Invalid free. **Distinct from F41**: F41's bug is INSIDE
   `CheckOwnedFunctionPointerType`'s param loop (only-outer); here the check function is never
   REACHED because the DISPATCH gate (`isFunctionPointerType()`) is false for ptr-to-fnptr.
   Different code site (SemaExpr.cpp:10329 gate vs SemaBSCOwnership.cpp:472 loop), different fix.
2. ptr-to-fnptr nullability-param variance — same dispatch miss, nullability dimension (distinct
   from F29 which is the param-loop-missing-nullability INSIDE the reached check).
3. ptr-to-fnptr borrow variance — `BorrowParamTypesMatch` would catch it if reached, but dispatch skips.

**Probe plan**: differential. Baseline = single-level fnptr `void(*)(int *_Owned)` ← raw fnptr
(F41 territory; or a clean reject if outer-level). Probe = ptr-to-fnptr `void(**)(int *_Owned)`
← ptr-to-raw-fnptr. If baseline rejects/handles but ptr-to-fnptr silently accepts → dispatch gate FN.

## IsOwnedRawPointerCastDisallowed (SemaBSCOwnership.cpp:256-270) — 2026-05-29, NOT a bug surface
**Invariant**: DIAGNOSTIC-SELECTION helper — returns true iff the cast is owned↔raw (outer level),
called ONLY from CheckOwnedQualTypeCStyleCast(:333) AFTER the cast is already rejected, to choose
err_owned_raw_cast_disallowed vs the generic err_owned_qualcheck_incompatible message.
**Not soundness-relevant**: runs post-rejection; only affects which error TEXT shows. Outer-only
check → a nested owned↔raw cast gets the generic message instead of the specific one (cosmetic
diagnostic-wording, LOW, not fileable). No probe-worthy candidate.

## CheckBorrowFunctionPointerType nested-fnptr _Borrow FN (SemaBSCOwnership.cpp:857-925) — 2026-05-29 Chain-D — CONFIRMED-NEW (pending file)

**Invariant**: at fnptr ASSIGNMENT time, if LHS/RHS fnptr params (or return) differ
in a `_Borrow` qualifier at ANY nesting depth, the assignment must be rejected.

**Body**: delegates each param/return to the `BorrowParamTypesMatch` lambda (:888),
which calls `DoPointerTypesSatisfyAssignmentConstraintsStrict` then, for pointer
params, compares pointees with canonical-unqualified equality after stripping
local owned/borrow (:896-905). The early-return guard at :883 uses
`hasBorrowRetOrParams()` (`hasBorrow()` -> `hasBorrowFields()`).

**FN (root)**: when a param is itself a FUNCTION POINTER, the recursion does NOT
descend into the inner fnptr's params. Two sub-causes, both confirmed:
1. `hasBorrowRetOrParams()` (TypeBSC.cpp:121) -> `Type::hasBorrowFields()`
   (TypeBSC.cpp:92) only walks RecordType/PointerType in the CanonicalType switch
   — a FunctionProtoType pointee falls through -> false. So the :883 early-return
   fires and skips the entire check for a fnptr param whose only borrow is nested.
2. Even when the guard is forced false (extra outer-level _Borrow param), the
   per-param `BorrowParamTypesMatch` still passes: for a fnptr param,
   `DoPointerTypesSatisfyAssignmentConstraintsImpl` (SemaBSCSafeZone.cpp:395-492)
   has `AreBSCPointerQualifiersCompatible` (:369) check only the OUTER fnptr's
   owned/borrow, then line 482 compares the FunctionProtoType pointees with
   `getCanonicalType().getUnqualifiedType()` equality, which treats
   `void(int *_Borrow)` == `void(int *)` here. No recursion into nested fnptr params.

**Asymmetry baseline**: single-level `cb_borrow_t fp = use_raw;` (LHS param directly
`int *_Borrow`) is correctly REJECTED ("incompatible _Borrow function pointer types").
Nested form `outer_borrow_t fp = takes_raw_cb;` (borrow buried in inner fnptr param)
compiles CLEAN.

**Distinct from**: F53 (redecl gate HasDiffBorrorOrOwnedQualifiers — REDECL not
assignment, different function/caller), F41 (CheckOwnedFunctionPointerType — OWNED
dimension, different function), F74 (call-site dispatch isFunctionPointerType
ptr-to-fnptr), F29/F56 (nullability), F73 (ensure_init). Distinct from the
2026-05-20 INCONCLUSIVE probe (pointer-to-borrow-pointer param, correctly rejected
because borrow is on the immediate pointee).

**Repro**: /tmp/explorer_probe_FINAL.KGosHq.cbs. Baseline: /tmp/explorer_baseline_FINAL.nqWpFe.cbs.
**Defect class**: C1 (peer-asymmetry / insufficient recursion depth — borrow variance
recurses into a plain pointer-typed param but not into a fnptr-typed param).
**Return-type variant SHAPE-REJECTED** (BSC grammar forbids `_Borrow` return with no borrow param).

## CheckMoveVarMemoryLeak MemberExpr arm — field-TYPE predicate asymmetry (SemaBSCOwnership.cpp:555-557) — 2026-05-30 UNPROBED

**In-scope reachability**: `CheckMoveVarMemoryLeak` is called from SemaStmt.cpp:3938 on
EVERY BSC return value, SemaExpr.cpp call-arg, parser assignment-RHS. NOT gated on
`_Owned struct` (OOS). The MemberExpr arm fires for a plain `struct S { int *_Owned f; }`
(struct merely HAS an owned field — IN SCOPE, F61/F67/F75 territory).

**Invariant**: moving any move-class field out of a `_Borrow`-reached struct must
be rejected (`err_move_borrow`) — the borrow holder doesn't own the resource.

**Asymmetry (root)**: the MemberExpr arm tests `ME->getType().isOwnedQualified()`
ONLY. Its sibling `CheckTemporaryVarMemoryLeak` (:537) tests
`isOwnedQualified() || isMoveSemanticType()`. A field whose TYPE is a nested struct
that HAS owned fields (`struct Inner { int *_Owned p; }` used as a field
`struct Outer { struct Inner inner; }`) is `isMoveSemanticType()` == true but
`isOwnedQualified()` == false. So `return s->inner;` / `int_owned q = s->inner` where
`s` is `_Borrow struct Outer*` is NOT diagnosed — the field-type predicate misses it.

**Distinct from F21** (getBase() walk gap for `(*s).f` paren-deref — base-type, this
is field-TYPE). **Distinct from F62** (SafeExpr wrapper). **Distinct from F61/F67/F75**
(dataflow analyzer null/double-free of owned FIELDS, different file BSCOwnership.cpp).
This is the Sema-time `err_move_borrow` gate's field-type predicate, an asymmetry
with the sibling temp-leak predicate.

**Candidates**:
1. **TOP**: move nested move-semantic field out of `_Borrow` struct → silently accepted →
   double-free at runtime (borrow holder + original owner both free). UNPROBED → probing.
2. The UO_Deref arm (:551) checks `UO->getType().isOwnedQualified()` — same single-predicate
   asymmetry for `*p` where `*p` is a move-semantic struct value (sibling).

## Chain Q — Borrow-pointer COMPARE + REBORROW checks (2026-05-30, bsc-explorer) — TRACED, NEGATIVE

Three named predicates; mapped callers + reachability:

### CheckBorrowQualTypeCompare (SemaBSCOwnership.cpp:818-830) — SOUND for purpose
**Caller**: SemaExpr.cpp:13533 (CheckCompareOperands), inside the
`else if (LHSType->isPointerType() && RHSType->isPointerType())` branch — runs
ONLY when BOTH operands are pointers.
**Invariant**: a `==/!=/<` between two pointers is well-typed iff borrow-ness
matches (`LHSBorrow != RHSBorrow → reject`); if neither is borrow → allow (plain-C
path); if both borrow → pointees must match UNQUALIFIED.
**Key property**: a comparison is READ-ONLY. Even if the gate accepts a
type-meaningless pairing, `==/<` neither moves, frees, nor stores → no memory UB.
So the gate's blast radius is bounded to "confusing bool", not soundness.
**Cells tabulated (all sound/shape-rejected)**:
- borrow == raw → REJECTED ("incompatible _Borrow types ... cannot be compared"). ✓
- borrow == borrow, same pointee → allowed. ✓
- borrow == const-borrow (pointee const differs) → allowed (gate strips pointee
  const via `getUnqualifiedType()`); read-only, sound. ✓
- `int*_Owned*` == `int*_Borrow*` (OUTER raw, nested owned vs borrow) → allowed
  because `!LHSBorrow` short-circuits BEFORE any pointee compare. The nested
  owned/borrow distinction IS laundered — but the comparison is read-only, so no
  consequence. The acceptance does not leak into any mutating context.
- `int*_Borrow*_Borrow` → SHAPE-REJECTED by the type system ("cannot be qualified
  by _Borrow" — no borrow-of-borrow direct type).

### CheckNeedReborrowPointerType (SemaExpr.cpp:6557-6569) — OUT OF SCOPE (member-only)
### CheckNeedCastQualifiedType (SemaExpr.cpp:6543-6555) — OUT OF SCOPE (member-only)
**Sole caller**: SemaExpr.cpp:6694/6719 in `GatherArgumentsForCall`, ONLY on the
implicit `this` argument of a BSC INSTANCE MEMBER function call
(`IsBSCInstanceFunc && MemberExpr base`). Member functions are OUT OF SCOPE
("no member func bullshit"). These two hops are unreachable from in-scope BSC.

### User-written reborrow `&_Mut *p` / `&_Const *p` (GetBorrowAddressOperandQualType, SemaExpr.cpp:15327-15405) — SOUND
This is the in-scope reborrow surface (not one of Chain Q's 3 named predicates,
but the user-facing reborrow). `&_Mut *p` recognized via `IsAddrBorrowDerefOp`
(literal `UO_Deref` operand). The const/mut soundness check at :15337-15343:
if `p`'s pointee `isConstQualified()` → `err_mut_expr_unmodifiable`. Confirmed:
`isConstBorrow()` (TypeBSC.cpp:548) == "pointee is const", so the `_Const`/`_Mut`
distinction IS the pointee's C const → the check is complete.
**Cells (all rejected correctly + defense-in-depth)**:
- `&_Mut *cp` (cp = `const int*_Borrow`) → REJECTED. ✓
- `&_Mut *cp` typedef-hidden const (`typedef const int CI; CI*_Borrow`) → REJECTED. ✓
- `&_Mut (*cp)` parenthesized deref → REJECTED (even if `IsAddrBorrowDerefOp` fails
  to recognize the paren-wrapped deref, the result type `const int*_Borrow → int
  *_Borrow` conversion is INDEPENDENTLY rejected in the safe zone → defense in depth).
- `&_Mut cp[0]` (const-element `_ArrayElem` borrow) → REJECTED. ✓
- `&_Mut *p` (p = mut `int*_Borrow`) → ALLOWED (baseline). ✓

**Verdict**: Chain Q SATURATED @ 28656aa9. Comparison gate is read-only (no
memory-unsoundness surface); reborrow type predicates are member-only (OOS);
user-written `&_Mut*` reborrow is sound with defense-in-depth on the const/mut
distinction. No new root cause.

## CheckOwnedOrIndirectOwnedType (SemaBSCOwnership.cpp:110-126) — PROBED-confirmed-F95 (static-local gap)
**Invariant**: forbids `_Owned` (direct :116, via typedef :119, or INDIRECT via
`isMoveSemanticType` :122 — the robust recursive predicate that catches
struct-containing-owned AND array-of-owned-struct, unlike the weaker
RecordType::hasOwnedFields) in positions that cannot track destruction.
**Call sites (the 3 gated positions)**: array element (SemaType.cpp:5202), GLOBAL
variable (SemaDecl.cpp:8527), UNION field (SemaDecl.cpp:18687).
**Peers**: `isMoveSemanticType` (trustworthy), `hasOwnedFields` (weaker, array-blind).
**Candidates**:
1. **static LOCAL owned-containing struct — PROBED-confirmed-F95 (HIGH)**.
   `static struct S s; s.p=mk();` compiles clean + valgrind 2-alloc/0-free leak.
   `static` defeats the owned-field leak analysis AND there is no static-local
   CheckOwnedOrIndirectOwnedType call site. Differential: plain local → "field
   memory leak" REJECTED; global → gate REJECTED. repro/F95_static_local_owned_field_leak.cbs.
   thread_local variant = UNPROBED sibling (likely same root, FOLD).
2. **thread_local** variant of (1). UNPROBED.
3. owned in a typedef'd array `typedef struct S SA[2]` then `SA x;` — canonical
   should still catch via isMoveSemanticType. UNPROBED.

## _Thread_local global owned gate (F95 storage-class sibling) — probing
**Invariant**: a `_Thread_local` global owned-containing var must be gated by
CheckOwnedOrIndirectOwnedType like a regular global (static storage, can't track destruction).
**Peers**: F95 (static-local gap), CheckOwnedOrIndirectOwnedType (global/array/union sites).
**Candidates**:
1. **`_Thread_local struct S g` (owned field) → rejected like regular global? — probing** (FOLD-F95 if gap).

## owned temp in comparison operand (16556 gate) — probing
**Invariant**: an owned temp as a `==`/`!=` operand, discarded, must be caught
(CheckTemporaryVarMemoryLeak called for comparison operands at SemaExpr.cpp:16555-57).
**Candidates**: 1. `mk() == nullptr` discarded → leak caught? (else FOLD-F14/F47).

## CheckOwnedQualifierOnNonPointerType (SemaBSCOwnership.cpp:189-243) — UNPROBED 2026-06-18

**Invariant**: the `_Owned` qualifier may only sit on a pointer type, an owned-structure
type, an owned-template-specialization, or a dependent type. `_Owned int x;`
(`_Owned` on a scalar) must be REJECTED (`err_owned_qualifier_non_pointer`).

**Body** — two ad-hoc checks, NOT a worklist (contrast `CheckArrayElemQualifierRules`
:69-106 which DOES use a proper worklist):
- First Check (:214-225): strip ALL pointer levels (loop :215-216) then AT MOST ONE
  array level (single `if` :217), reach base, reject if base `isOwnedQualified()` &&
  not a valid owned type.
- Second Check (:230-242): if T itself isn't a valid owned type, strip at most one
  array level then one pointer level, reject if that `isOwnedQualified()`.

**Peers**: `CheckArrayElemQualifierRules` (:69, the `_ArrayElem` sibling — uses a real
worklist that walks BOTH pointer pointees AND array elements recursively, so it
catches arbitrary nesting). `CheckInstantiatedTypeOwnedQualifiers` (:139, the
post-instantiation owned-placement gate — also single-level `isValidOwnedType(T)`,
no deep strip). `CheckOwnedOrIndirectOwnedType` (:110, F80/F95).

**Asymmetry (root)**: the strip logic in both checks is SHALLOW and ad-hoc — it
handles at most one array level. A type that interleaves array+pointer nesting deeper
than the hard-coded strip depth (e.g. `_Owned int **a[3]` = array-of-ptr-to-ptr-to-
owned-scalar, or `_Owned int a[2][3]` = 2-D array of owned scalar) can place `_Owned`
on a NON-pointer base while escaping both checks. The `_ArrayElem` peer's worklist
would catch it; this gate's hand-rolled strip does not.

**Candidates (ranked)**:
1. `_Owned int a[2][3];` — 2-D array of owned scalar → **PROBED-REJECTED** (the
   `_Owned int[3]` array form gets caught by a different "array cannot be qualified
   by _Owned" path). Not the gap.
2. **CONFIRMED-NEW** `_Owned int **a[3];` — array of ptr-to-ptr-to-(_Owned int).
   The `_Owned` parks on the base scalar `int`; both ad-hoc strip checks miss it →
   ACCEPTED (exit 0, no diag). Sibling deeper forms `_Owned int ***a[3];` (3 ptr) and
   `_Owned int *a[3][3];` (1 ptr + 2-D array) ALSO accepted.
3. (composition) the malformed type is LIVE: downstream `**a[0]` read emits
   "cannot cast '_Owned int' to 'int'" — the dataflow treats the base int as owned.

### CONFIRMED-NEW 2026-06-18 (bsc-explorer) — owned-placement gate shallow-strip FN
**Root cause**: `CheckOwnedQualifierOnNonPointerType` (SemaBSCOwnership.cpp:189-243)
uses two hand-rolled strip checks instead of a worklist. First Check (:215-217) strips
ALL pointer levels then AT MOST ONE array level; Second Check (:230-242) strips at most
one array + one pointer. A type that interleaves an ARRAY level with >=2 POINTER levels
(`_Owned int **a[3]` = `_Owned int **[3]`) places `_Owned` on the base scalar `int`
beyond the strip depth → both checks miss → the invalid placement is ACCEPTED.
**Asymmetry baseline**: `_Owned int *a[3];` (drop one `*`) and `_Owned int **p;`
(drop the array) are BOTH correctly REJECTED ("type of 'int' cannot be qualified by
'_Owned'", err_owned_qualifier_non_pointer). The repro differs from the baseline by
exactly one `*`.
**Distinct from**: F80/F95 (`CheckOwnedOrIndirectOwnedType` indirect-owned gate, a
DIFFERENT function + different invariant — that's the global/static/array decl-position
gate; this is the `_Owned`-on-non-pointer-base placement gate). G10 (generic-alias-to-
array substitution-path skip — a different path entirely). The simple `_Owned int x;`
crash-recovery cases (X01-X20) only ever probed the SHALLOW rejected forms.
**Defect class**: C1 — the `_ArrayElem` peer `CheckArrayElemQualifierRules` (:69-106)
uses a COMPLETE worklist (walks both pointer pointees AND array elements), catching
arbitrary nesting; the owned-placement gate picked ad-hoc shallow strips. Wrong
recursion-depth predicate selected for the owned dimension.
**Fix surface**: replace the two ad-hoc strip checks with a worklist (mirror
`CheckArrayElemQualifierRules`): walk through every pointer pointee and array element,
and reject if any reached NON-pointer base is `isOwnedQualified()`.
**Repro**: /tmp/explorer_repro_FINAL.J8MFl5.cbs. Baseline:
/tmp/explorer_baseline_FINAL.9eyFtv.cbs.

## owned temp passed directly to consumer — probing
**Invariant**: `consume(mk())` — an owned temp flowing directly into a consuming
function is consumed, not leaked; compiles clean, valgrind balanced (1 alloc/1 free).
**Candidates**: 1. consume(mk()) → clean + balanced (not a false leak)?

## CheckMoveVarMemoryLeak — wrapper/inner-expr dispatch enumeration (2026-06-23 bsc-explorer)
**Source** (SemaBSCOwnership.cpp:547-559): after `E->IgnoreParenCastsSafe()` (which
peels ParenExpr, GenericSelectionExpr, ChooseExpr, MaterializeTemporaryExpr, FullExpr,
UO_Extension, ConstantExpr, all CastExpr including CStyleCast/Implicit, AND SafeExpr),
the dispatch tests ONLY:
  - `isa<UnaryOperator>` with `UO_Deref` whose type is owned-qualified AND subexpr type is borrow-qualified
  - `isa<MemberExpr>` whose type is owned-qualified AND `getBase()->getType().isBorrowQualified()`
**Inner-expr kinds NOT dispatched** (after strip, these fall through = no `err_move_borrow`):
  - `ArraySubscriptExpr` — `(*s).f[0]` / `s->f[0]` where the subscript result is an owned
    value moving out of a `_Borrow`-reached struct. Not isa<UnaryOperator> nor isa<MemberExpr>.
  - `BinaryOperator` (BO_Comma) — `IgnoredExpr`/ExprWithCleanups not peeled by `IgnoreParensSingleStep`.
  - `ConditionalOperator` / `BinaryConditionalOperator` — not peeled (would pick an arm; not single-child).
**Note**: `IgnoreParenCastsSafe` now strips `SafeExpr` (the F62 gap is FIXED in 34e6f26e).
**Peers**: `CheckTemporaryVarMemoryLeak` (:534) uses raw `dyn_cast<CallExpr>` (F14/F47/F20).
**Candidates**:
1. **ArraySubscriptExpr move-through-borrow** — PROBED SHAPE-REJECTED: direct `_Borrow`
   pointer subscript is forbidden ("subscript of _Borrow pointer is not allowed"); array-
   of-owned directly forbidden ("type of array cannot be qualified by '_Owned'"); array-of-
   owned-bearing-struct forbidden at the array decl gate (CheckOwnedOrIndirectOwnedType). The
   subscript-yields-owned move surface is unreachable from valid BSC. /tmp/explorer_probe_arrsub.1mm8rN.cbs
2. **Chained MemberExpr `s->a.p` move-out-of-borrow** — PROBED FOLDED-F21: compiles clean,
   valgrind double-free (1 alloc/2 frees/Invalid free), but the base is `s->a` (MemberExpr
   whose type is struct A, not borrow-qualified) — SAME shallow `getBase()->getType()`
   root as F21; F21's proposed base-walk (recurse through MemberExpr chains) covers it.
   /tmp/explorer_chain.KxYr5a.cbs (runtime), baseline /tmp/explorer_baseline_arrsub.IvEW9j.cbs
3. ConditionalOperator arm of owned move — FOLDED-F25 (already filed).

## CheckTemporaryVarMemoryLeak call-site gate — `isComparisonOp` (SemaExpr.cpp:16547) — PROBED FOLDED-F22
**Site**: SemaExpr.cpp:16542-16551 (inside `BuildBinOp`-area owned-pointer binary-op handler).
After `DiagnoseOwnedPointerBinaryOp` (which early-returns/allows BO_LAnd/LOr/Comma), the
temp-leak check is gated: `if (BinaryOperator::isComparisonOp(Opc)) { CheckTemporaryVarMemoryLeak(LHS); (RHS); }`.
So an owned-pointer temporary on either side of `&&`/`||`/`,` is NEVER run through the
temp-leak predicate at this site.
**Asymmetry PROVEN**: `b = (mk() == 0)` → DIAGNOSED "memory leak because temporary
variable 'mk()'"; `b = (mk() && c)` → compiles CLEAN, valgrind 1 alloc/0 frees (leak).
`||` variant also leaks (same gate).
**DEDUP**: FOLDED-F22. F22's own catalog explicitly states its surface = "wherever a
leaking-temp can appear without going through `ActOnExprStmt` AND without triggering the
comparison-op branch at SemaExpr.cpp:16557." The `&&`/`||` operand position is exactly
that — on the "doesn't trigger the comparison-op branch" side. F22's fix (invoke
CheckTemporaryVarMemoryLeak from each expression-acceptor / a CheckFullExpressionLeak
helper) covers it. NOT a new root cause.
**Probes**: /tmp/explorer_repro_land.63iey4.cbs (&& leak), /tmp/explorer_base_land.MZPxxY.cbs
(== diag control), /tmp/explorer_lor.GR1F2A.cbs (|| leak), /tmp/explorer_diff.WOpJS2.cbs
(combined diff).


## indirect-owned/borrow guard call-site enumeration — COMPLETE 2026-06-25 (monomorph-bypass surface closed)
CheckOwnedOrIndirectOwnedType / CheckBorrowOrIndirectBorrowType (SemaBSCOwnership.cpp:110 / :991) — the "owned
/borrow may not (indirectly) qualify X" guard. EXACTLY 3 call sites (grep-verified, no others):
1. SemaType.cpp:5202  "array"        — RE-RUN at monomorph (type-construction path) → SOUND (array-of-owned via
   generic correctly rejected; confirmed incl. inside generic union).
2. SemaDecl.cpp:18723  "union field"  — NOT re-run at monomorph → G17 (FILED). union U<int*_Owned> accepted while
   direct rejected; constructible+tracked in CHECKED context → conformance MEDIUM.
3. SemaDecl.cpp:8556    VarEnv (var declarator, isFileContext-gated) — NOT re-run at monomorph → F82 (LOW, folded).
   static-local/global owned via generic; UNSAFE-only (the _Safe init/nonnull/non-constant rules independently
   reject it), so no _Safe soundness hole. The F95 fix (direct static-local-owned reject) is also not re-applied
   at monomorph — same site.
COMPLETENESS: these 3 are the ENTIRE indirect-owned/borrow guard surface. The SemaType site re-runs at monomorph;
the two SemaDecl sites do not. Only the union site (G17) is a filable checked-context bug; the var site folds to
F82. NO undiscovered G17-sibling exists in this family. Monomorph-guard-bypass search CLOSED for indirect-owned.

## CheckInstantiatedType{Owned,Borrow}Qualifiers (SemaBSCOwnership.cpp:139/161) — read 2026-06-25
INVARIANT: the MONOMORPH-time type-validity check — an owned/borrow template param must instantiate to a VALID
owned/borrow type (owned→pointer/owned-struct/owned-tmpl-spec; borrow→not-a-function-pointer). Callers:
SemaTemplateInstantiateDecl.cpp:1121/1250 (var/field), 2110 (fn return), 2125 (fn param).
C1 SIBLING-ASYMMETRY (found, then REFUTED): the OWNED check (:151) rejects owned on ANY non-valid type via the
positive `isValidOwnedType`; the BORROW check (:163) rejects ONLY `isBorrowQualified() && isFunctionPointerType()`
— narrower. CANDIDATE: borrow-on-non-pointer (int/struct) escapes the monomorph check. REFUTED: COMPENSATED by the
SemaType construction guard `err_typecheck_invalid_borrow_not_pointer` (SemaType.cpp:2059) which fires at TYPE
CONSTRUCTION (runs at monomorph): `Box<int>{int _Borrow v}` → "_Borrow type requires a pointer or reference
('int' is invalid)". So net coverage is COMPLETE & symmetric in effect: borrow/owned on int|struct → SemaType
guard; on function-pointer → "cannot be qualified by '_Borrow'/'_Owned'" (verified via generic, both rejected);
on valid pointer → allowed. The narrow monomorph borrow-check is intentional (only the fnptr case, which the
general not-a-pointer guard misses since a fnptr IS a pointer). NO gap. SOUND.

## CheckMoveVarMemoryLeak (SemaBSCOwnership.cpp:575) — move-out-of-borrow check (2026-06-27)
INVARIANT: emits err_move_borrow when moving an _Owned out of a _Borrow — handles two TOP-level forms (after
IgnoreParenCastsSafe): UnaryOperator UO_Deref (`*b`, result Owned + subexpr Borrow) and MemberExpr (`b.f`, member Owned +
base Borrow). PEERS: CheckTemporaryVarMemoryLeak (:534, F118/F121), the borrow-checker loan path (F119). CANDIDATES:
1. (non-cast wrappers not peeled) ternary/comma/logical wrapping `*b` → top expr not UnaryOp → slips = F25 (ternary, filed),
   F21 (paren-deref, filed), F79 (move-semantic field, filed). Same root: incomplete top-form/wrapper coverage. PROBED-confirmed.
2. (only UnaryDeref+MemberExpr; ArraySubscript NOT handled) `(*b)[i]` move of an owned array element through a borrow →
   SHAPE-REJECTED: arrays-of-owned forbidden ("type of array cannot be qualified by _Owned") → unreachable. Move-through-borrow cluster bounded (access forms covered; gaps = wrappers F21/F25/F79).
3. (IgnoreParenCastsSafe peels paren/cast/SafeExpr) — covered; other wrappers (cand 1) slip.

## OwnershipImpl::merge (BSCOwnership.cpp:231) — branch-join state merge = F75 ROOT (2026-06-27, precise)
INVARIANT(broken): merges two branch states. statsA empty → return statsB (identity, sound). Else: var-status bitvectors
UNIONED via `statsA.OPSStatus[VD] |= BV` (OR — sets Moved bit if moved on EITHER branch → has(Moved) flags non-field use,
CORRECT); BUT the owned-field SETS (OPSOwnedOwnedFields / SOwnedOwnedFields / SNullOwnedFields) are UNIONED via `insert`
(field owned on EITHER branch stays in the owned-set at join) — this is F75: a field moved on one branch + owned on other →
join treats it owned → consume-after = double-free. The owned-field sets should be INTERSECTED (MEET: owned at join iff owned
on BOTH), not unioned. PEERS: the check families consume these sets. CANDIDATES: 1. F75 (OwnedOwnedFields union, filed). 2.
SNullOwnedFields union = F45 (stale-null-field double-free, open). 3. non-field var: |= OR + has(Moved) → CAUGHT (bounds F75 to fields). UNPROBED→probing 3.

## is/has status predicates (BSCOwnership.cpp:343/380) — definite vs maybe status (2026-06-27)
INVARIANT: is(VD,S) = S is the ONLY bit set (test S, reset, !any() → definitely-and-only S); has(VD,S) = bit S set among
possibly others (maybe-S). After merge `|=` OR, a maybe-moved var has Moved+Owned bits → is(Moved)=false, has(Moved)=true.
The VAR-level use-after-move check uses has(Moved) (→ non-field maybe-moved CAUGHT, baseline sound); the FIELD-level check
uses OwnedOwnedFields-set membership (unioned at merge → over-permissive = F75). This is WHY F75 is field-only. CANDIDATES:
1. switch-FALLTHROUGH double-consume (consume in case1, fall to case2 consume) → has(Moved) at case2 catches it? UNPROBED→probing.
2. is/has bit semantics sound. 3. multi-bit status edge.

## checkMemoryLeak (BSCOwnership.cpp:1941) — leak-at-scope-end detection (2026-06-27)
INVARIANT: at scope/iter end, if !canAssign(VD) (VD still Owned, not Moved/Uninit → reassigning would leak) → MemoryLeak +
reset state (resetAll+set Moved — important for for/while to avoid re-report); if OPSOwnedOwnedFields/SOwnedOwnedFields[VD]
non-empty → FieldMemoryLeak (remaining owned fields). Owned-struct (OOS) skipped for S-field leak. PEERS: canAssign, the
merge (F75 leaves a field in OwnedOwnedFields → that's double-free not leak). CANDIDATES: 1. continue/break-skip-consume leak
→ owned at iter-end caught? PROBED-sound (continue-skip → "memory leak"). 2. field-leak via OwnedOwnedFields sound. 3. loop
state-reset prevents re-report (sound). Leak detection sound.

## canAssign (BSCOwnership.cpp:417) — "no live owned value" predicate (2026-06-27)
INVARIANT: canAssign(VD)=true iff VD's status bitvector, after clearing {Uninitialized,Moved,Null}, has NO bits left (i.e.,
VD holds no live owned value → reassigning won't leak). Used by checkMemoryLeak (!canAssign → MemoryLeak) + assignment checks.
If Owned bit set → canAssign false → would-leak. PEERS: checkMemoryLeak(:1941), is/has(:343/380). CANDIDATES: 1. partial-field
-consume: struct with 2 owned fields, consume one, other unconsumed at scope-end → FieldMemoryLeak caught? 2. canAssign bit
logic sound. 3. BOP/S-status variants. UNPROBED→probing 1.

## CheckMoveVarMemoryLeak (SemaBSCOwnership.cpp:575-587) — move-out-of-borrow gate
- **Invariant**: moving an `_Owned` value OUT of a `_Borrow` (the borrow doesn't own it) must be rejected (`err_move_borrow`), else the owner double-frees.
- **Peers**: CheckTemporaryVarMemoryLeak (sibling, same file, same whitelist anti-pattern → F118 broad-incompleteness), getMemberFullField (F46 wrapper-peel).
- **Structure**: a 2-case whitelist — `UnaryOperator UO_Deref` (579: `*b` where pointee owned-qual + subexpr borrow-qual) + `MemberExpr` (583: `s->f` where field owned-qual + base borrow-qual). Everything else → no check.
- **Candidates**: (1) **PROBED-F21**: MemberExpr base not deref-peeled — `(*s).f` has base `*s` (UnaryDeref, type=struct not borrow) → `getBase()->getType().isBorrowQualified()` false → missed (filed F21, live). (2) **PROBED-F79**: both arms test `isOwnedQualified()` only — a move-semantic struct field (struct-with-owned-field, not directly owned-qual) slips (filed F79, live). (3) **ArraySubscriptExpr not handled** — would let `consume(arr[i])` move an owned element out of a borrowed array, BUT owned arrays are language-forbidden ("type of array cannot be qualified by _Owned") so no borrowed-owned-array container exists → SHAPE-BLOCKED (see probe).

## Sema::CheckBSCQualTypeAssignment (SemaBSCOwnership.cpp:482) — assignment type-compatibility
- **Invariant**: an assignment whose LHS/RHS carry owned or borrow qualifiers (directly or via pointee fields) must pass CheckOwned/BorrowQualTypeAssignment; both run if both flags set.
- **Peers**: CheckOwnedQualTypeAssignment, CheckBorrowQualTypeAssignment, hasOwnedFields/hasBorrowFields (chain AG/F81 — array-blind), CheckBSCFunctionPointerType.
- **Candidates**: (1) **F81-consumer (folds)**: `MayHaveBorrow |= LHSPtr->hasBorrowFields()` uses the array-blind predicate → a pointer to a struct-with-borrow-ARRAY-field would not set MayHaveBorrow → borrow check skipped (same F81 root; one array-aware fix closes it). Owned side gated (AG). (2) ordering LHS-borrow-first then owned (both run) — sound. (3) **PROBED-SOUND 2026-06-30**: non-pointer struct-VALUE assignment `s2=s1` (s1 has owned field) then `consume(s1.f)` → rc=1 "use of uninitialized value: s1.f" — the ownership analyzer (VisitBinAssign) moves s1's fields on the struct copy and catches the post-move use; this Sema type-check correctly defers the move-tracking to it.

## Sema::CheckBorrowQualTypeAssignment (SemaBSCOwnership.cpp:705) — borrow type-compatibility (recursive)
- **Invariant**: LHS/RHS borrow-qualification must match; `_ArrayElem` may be dropped (not added); void-borrow needs const-match; nested pointers recurse pointee-wise.
- **Peers**: CheckBSCQualTypeAssignment (caller), AreBSCPointerQualifiersCompatible (chain AE), F27 (const-cast FP), F76 (nested-fnptr borrow qualifier).
- **Candidates**: (1) `_ArrayElem`-drop branch (:713) consistent with chain AE (traced sound). (2) **PROBED-SOUND 2026-06-30**: `int*_Borrow`→`void*_Borrow` allowed rc=0 (const matches); `const int*_Borrow`→`void*_Borrow` (const-strip) → rc=1 "conversion ... forbidden in the safe zone" (const-mismatch caught). No const-strip FN. (3) trait-desugar branch (:719) = OOS.

## Sema::CheckOwnedOrIndirectOwnedType (SemaBSCOwnership.cpp:110) — owned placement gate (F80 root, F81 owned-twin)
- **Invariant**: reject a type qualified/containing `_Owned` in a no-owned-allowed placement (global/array-elem/union): direct owned-qual, owned typedef, or `isMoveSemanticType()` (indirect owned fields).
- **Peers**: CheckBorrowOrIndirectBorrowType (F81 — the borrow twin, MISSING at array declarator), isMoveSemanticType (array-blind), F80 (indirect-owned array gate).
- **Candidates**: (1) the ownedFields branch uses `isMoveSemanticType()` which is ARRAY-BLIND (chain AG) — but the gate is comprehensive because SemaType.cpp calls it on the ELEMENT type after array-decay (bypassing the array-blindness); the borrow twin lacks that element-type call = F81 D1. (2) **PROBED-SOUND 2026-07-01**: `_Safe void g(struct O arr[2]);` (param array-of-owned-field-struct) → rc=1 "type of array cannot be qualified by '_Owned'(even indirectly)". Owned gate comprehensive at PARAM too — every position gated (variable/global/field/union/return/param). Confirms F81 (borrow twin) is the sole gap. (3) isMoveSemanticType on a pointer-to-owned-field-struct param (pointer not move-semantic → gate may not fire; but that is a legit pointer param).

## Sema::CheckBorrowOrIndirectBorrowType (SemaBSCOwnership.cpp:1019) — borrow placement gate (F81 root, source-confirmed)
- **Invariant**: reject a `_Borrow`-qualified/containing type in a no-lifetime placement (global/array-elem/union): direct, typedef, or `hasBorrowFields()`.
- **Peers**: CheckOwnedOrIndirectOwnedType (comprehensive owned twin), hasBorrowFields (array-blind, F81 D2), CheckBorrowFunctionType (return-type path, F81 V7).
- **Candidates (F81 CONFIRMED)**: (1) uses **array-blind `hasBorrowFields`** (D2) — misses array-of-borrow-field-struct. (2) `who_calls` = only global-var(:8557) + union-field(:18724); MISSING the SemaType array-declarator element-type call the owned gate has (D1). Both = F81's 8-position blast radius; two-part fix (ArrayType arm + array-declarator call). (3) **PROBED-SOUND 2026-07-01**: non-array borrow-field struct PARAM `_Safe void g(struct B b)` → rc=0 ALLOWED (correct — a param has a caller-provided lifetime source, unlike a no-lifetime global). The gate correctly distinguishes no-lifetime slots (global/array-elem/union = gated) from lifetime-bearing positions (params = allowed). F81 is the array-declarator gap in the no-lifetime slots.

## Sema::CheckNestedBorrowType (SemaBSCOwnership.cpp:1036) — nested-borrow TYPE check
- **Invariant**: a nested-borrow type (borrow pointing to a borrow, or borrow-containing pointee) is rejected at type formation (SemaType.cpp:2120) — isNestedBorrow(T) classifies.
- **Peers**: isNestedBorrow, CheckBorrowOrIndirectBorrowType (placement gate), reborrow (ActionExtract, runtime — distinct from this TYPE check).
- **Candidates**: (1)(2) **PROBED-SOUND 2026-07-01**: `int*_Borrow*_Borrow` → rc=1 "type of 'int *_Borrow' cannot be qualified by '_Borrow'"; typedef'd `BI *_Borrow` (BI=int*_Borrow) → rc=1 (canonical-type-based, sees through typedef). Nested-borrow type rejection sound incl. typedef. (3) distinct from runtime reborrow `&_Mut *b` (allowed, sound) — this is the static TYPE forbiddance.
