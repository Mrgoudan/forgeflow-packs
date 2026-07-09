# WalkerBSC.h notes

Path: `clang/include/clang/AST/BSC/WalkerBSC.h`. Two walker classes share a
StmtVisitor / DeclVisitor / TypeVisitor pattern. Result feeds a routing /
gating decision in a single caller for each.

## Class inventory

| Class | Inherits | Caller | Returns-true effect |
|-------|---------|--------|---------------------|
| `BSCFeatureFinder` (48-363) | Stmt+Decl+TypeVisitor | `RewriteBSC.cpp:911-925` (and others) | Route through DeclPrinter (BSC-aware) vs verbatim source |
| `SafeFeatureFinder` (367-461) | Stmt+DeclVisitor only | `SemaDeclBSC.cpp:203` `Sema::FindSafeFeatures` → `BSCDataflowAnalysis:279 RequireBorrowCheck` | Run/skip the BORROW checker for this function |

F59 already filed: `BSCFeatureFinder` misses `UnaryExprOrTypeTraitExpr` →
rewriter emits `_Owned` qualifier literally to .c.

## SafeFeatureFinder::VisitQualType (367-380) — PROBED-inconclusive

**Invariant**: returns true iff `QT` has owned/borrow at the TOP level or
owned/borrow FIELDS in a struct. Pointee owned/borrow is not detected; nested
function-proto types are not detected; TypedefType / TypeAlias not unwrapped.

**Peers**: `BSCFeatureFinder::VisitQualType` (90-110) which DOES recurse
through pointer pointee, paren type, function-proto return/params, and
TypedefType / TemplateSpecializationType.

**Candidates**:
1. **Function-pointer parameter type with owned in the inner proto**:
   `void f(void (*cb)(int *_Owned))` — PROBED-inconclusive.
   Walker gap is REAL: `void (*)(int *_Owned)` has all four predicates
   false. `PointerType::hasOwnedFields()` calls Type::hasOwnedFields on
   the FunctionProtoType pointee, which only handles RecordType /
   PointerType → returns false. `BSCFeatureFinder::VisitType` recurses
   through FunctionProtoType params (lines 73-82); SafeFeatureFinder
   does not.
   However, observationally inert: any program that *uses* the fn-ptr
   in a way the borrow checker would diagnose (e.g. `cb(p); cb(p)` with
   p _Owned) requires `p` to be top-level _Owned somewhere, which
   triggers the walker via VisitVarDecl / param loop. Could not
   construct a body that (a) walker misses entirely, (b) borrow checker
   would have diagnosed.
   Probes attempted: probe.HWZ5D9 (in _Safe — fn-ptr call forbidden);
   probe.DRl4Br (out of _Safe — accepted, but didn't isolate gap);
   probe.7mgmRM (caller w/ factory — no use-after-move semantics).
2. **Owned via pointer-to-pointer** `int *_Owned *p` — PROBED-shape-rejected
   as a walker gap. `PointerType::hasOwnedFields()` (TypeBSC.cpp:57-66)
   recurses into pointee and returns TRUE when pointee is `int *_Owned`.
   So `int *_Owned *` IS detected via `hasOwnedFields()`. Probe attempted
   (probe.iUCDAb / probe.Al8YJ8): clean compile on `consume(*pp); consume(*pp)`
   regardless of whether a dummy local _Owned was added to force walker on.
   This means the diagnostic miss is in the OWNERSHIP ANALYZER / borrow
   checker (doesn't model `*pp` moves), not the walker. Different defect
   (BSCOwnership/BSCBorrowChecker, not WalkerBSC.h).
3. **Owned/borrow only inside `sizeof(int *_Owned)` operand** (mirror F59):
   `SafeFeatureFinder` lacks `VisitUnaryExprOrTypeTraitExpr` override, and
   `VisitStmt` walks children via `Stmt::children()` — but
   `UnaryExprOrTypeTraitExpr::children()` returns NO children when it's a
   type-operand (only when it's an expr-operand). So the type-operand is
   invisible to the walker. Borrow check skipped. (But sizeof is unevaluated
   so this is mostly a fold of F59 with a different consumer — could still be
   distinct if some Sema check is gated.)
4. **Owned via typedef alias**: `typedef int *_Owned OPtr; void f(OPtr p)`.
   May or may not depend on whether `isOwnedQualified` sees through a typedef.
   `BSCFeatureFinder` explicitly checks `TypedefType` — `SafeFeatureFinder`
   does not, but the underlying check could still trigger if the qualifier
   propagates.

## INCIDENTAL FINDING (not Walker, recorded for parent)

While probing `int *_Owned *pp` parameter (/tmp/explorer_probe.iUCDAb.cbs
and /tmp/explorer_probe.Al8YJ8.cbs), observed: `caller(int *_Owned *pp)
{ consume(*pp); consume(*pp); }` compiles WITHOUT a use-after-move
diagnostic. Baseline (same body with `int *_Owned p` flat param,
/tmp/explorer_probe.kb4oat.cbs) correctly diagnoses second consume(p).
This is a soundness gap in the OWNERSHIP ANALYZER (BSCOwnership.cpp):
move through deref of a pointer-to-owned-pointer is not tracked.
Root cause is NOT WalkerBSC.h (walker correctly returns true via
`PointerType::hasOwnedFields`).

Suggest filing under BSCOwnership domain. Repro:
- /tmp/explorer_probe.iUCDAb.cbs (caller with int *_Owned *pp, double consume(*pp))
- /tmp/explorer_probe.Al8YJ8.cbs (same + dummy local _Owned to force walker on; STILL clean — proves issue is in ownership analyzer's move tracking, not walker)
- /tmp/explorer_probe.kb4oat.cbs (baseline: flat int *_Owned param → correctly diagnosed)

NOTE: This out-of-focus finding should be checked against bug_log.md for
duplication before any filing decision. Other Explorers (BSCOwnership-deep)
may have already covered this surface.

## SafeFeatureFinder::VisitUnaryOperator (456-458) — UNPROBED

**Invariant**: returns true for `&mut`, `&const`, deref-mut, deref-const
unary ops. These are the borrow-creation operators.

**Peers**: `BSCFeatureFinder` has no `VisitUnaryOperator` (relies on type
detection to flag borrow features).

**Candidates**:
1. Body containing borrow operator inside a context where `VisitStmt` doesn't
   recurse — there is essentially no such gap because `VisitStmt` always
   walks `children()`. Likely not exploitable.
2. (lower priority — depth)

## R3 SafeFeatureFinder — gating mechanism mapped + all blind spots NEUTRALIZED (2026-05-30)

**Gating chain (SemaDeclBSC.cpp:279-411):** `RequireBorrowCheck =
FindSafeFeatures(FD)` -> `SafeFeatureFinder::FindOwnedOrBorrow`. ONLY the
CFG-based pipeline (`runNullabilityCheck` / `runOwnershipAnalysis` /
`BSCBorrowChecker`) rides this gate. Two facts that kill the "finder-skip
false-negative" thesis for pure `_Safe`:

- **Init analysis is gated SEPARATELY** (`RequireInitCheck`, line 308-329):
  `HasSafeZoneInFunction || SZ_Safe || ensure_init-param`. A `_Safe` function
  ALWAYS gets init analysis irrespective of the feature-finder.
- **The owned-temp-leak diagnostic is NOT gated.** `err_owned_temporary_memLeak`
  is emitted by `Sema::CheckTemporaryVarMemoryLeak` (SemaBSCOwnership.cpp:534),
  a plain Sema method run during statement checking. Same for owned/borrow
  assignment checks (`CheckBSCQualTypeAssignment`). So discard-an-owned-call and
  assign-owned leaks are caught regardless of flagging. The finder-skip can only
  hide **BSCBorrowChecker-exclusive** facts (use-after-move, dangling-borrow
  lifetime) — and every reachable owned/borrow obligation flags the finder.

**The three structural blind spots, each NEUTRALIZED:**

1. **Array-element recursion** (`Type::hasOwnedFields` / `RecordType::hasOwnedFields`
   never recurse into a `ConstantArrayType` element). UNREACHABLE: Sema forbids
   owned arrays at type formation — both direct (`int *_Owned arr[2]` ->
   "type of array cannot be qualified by '_Owned'") and indirect
   (`struct Inner{int *_Owned p;} a[2]` -> "...even indirectly, 'struct Inner'
   contains '_Owned' type"). PROBED-shape-rejected.
   (probes: explorer_probe_arr.OOvCrU, explorer_probe_arrstruct.myPQbO)

2. **Fn-proto recursion** (`SafeFeatureFinder::VisitQualType` doesn't call
   `VisitType`, so an owned/borrow inside a fn-ptr type's proto is missed —
   the gap `BSCFeatureFinder::VisitType` 73-82 covers). UNREACHABLE for a leak
   in pure `_Safe`: ANY function-pointer CALL is rejected
   "_Unsafe function call is forbidden in the safe zone" (confirmed with an
   owned-free `int (*fp)(int); fp(c);`). Without a call, the buried owned can't
   leak. Confirms + extends the prior candidate-1 "inconclusive". PROBED-shape-rejected.
   (probes: explorer_probe_fp.ilwKOo, explorer_probe_fpparam.eMmNOW)

3. **Bare-CallExpr result type not checked** (finder applies `VisitQualType`
   only to Decl types, init-expr types, CStyleCast types — never to a bare
   CallExpr's own `getType()`). Real gap in the finder, but the matching leak
   diagnostic is emitted by the NON-gated `CheckTemporaryVarMemoryLeak`, so
   `make(c);` (owned result discarded, no owned local/cast) is still rejected
   "memory leak because temporary variable 'make(c)' is _Owned". PROBED-folded
   (caught by a non-gated path, not the gated analyzer).
   (probe: explorer_probe_disc.jgMC4x)

4. **Typedef alias** (`typedef int *_Owned OPtr`) PROBED-shape-rejected as a
   walker gap: `isOwnedQualified()` reads the CANONICAL type, which is
   owned-qualified through the typedef, so `VisitQualType` flags it. The missing
   TypeAliasDecl shortcut (that `BSCFeatureFinder::VisitQualType` 101-105 has) is
   redundant for owned/borrow. (probe: explorer_probe_td.Qxj6T5)

**Verdict: NO new finder-skip false-negative.** F59 (BSCFeatureFinder/rewriter)
stays the sole WalkerBSC.h filing; the analogous SafeFeatureFinder gaps are all
either Sema-shape-rejected or covered by a non-gated Sema leak check. Ledger:
/tmp/probed_R3E5.md.

## BSCFeatureFinder::VisitType / VisitQualType (WalkerBSC.h:60-110) — probing ArrayType gap
**Invariant**: must return true for ANY type transitively containing a BSC feature
(owned/borrow/trait), so the rewriter pretty-prints (strips qualifiers) instead of
taking the verbatim-source path (which leaks `_Owned`/`_Borrow` into the `.c`).
**Peers**: `VisitStmt` sizeof gap = **F59** (same class, different method);
`VisitFunctionDecl` param loop (:141) catches param types.
**VisitType handles**: PointerType (pointee), ParenType (inner), FunctionProtoType
(ret+params), Trait. **MISSING: ArrayType** (no ConstantArray/getElementType
recursion) → `int *_Owned arr[N]` element-ownedness undetected (array isn't
owned-qualified and has no "fields", so VisitQualType's hasOwnedFields misses it).
**Candidates**:
1. **ArrayType-of-owned/borrow not recursed → rewriter verbatim leak — probing**.
   FOLD-RISK: F59 class (FeatureFinder incompleteness), distinct method/case.
2. AttributedType / AdjustedType not explicitly visited (getAs may strip). UNPROBED.
3. AtomicType `_Atomic(int*_Owned)` operand. UNPROBED (likely rare/SHAPE-REJECTED).

## SafeFeatureFinder::VisitQualType missing recursion → RequireBorrowCheck FN — probing
**Invariant**: FindSafeFeatures gates RequireBorrowCheck (SemaDeclBSC.cpp:356) = whether
the borrow/ownership check runs. SafeFeatureFinder::VisitQualType (WalkerBSC.h:374-380,
self-flagged TODO "Is this enough? Do we need VisitType API?") checks ONLY direct
owned/borrow qual + owned/borrow FIELDS — NO pointee/array recursion. An array-of-owned
`int *_Owned arr[N]` (array type: not owned-qualified, not a record w/ owned fields) may
be MISSED → borrow check skipped → leak/violation uncaught (HIGH FN).
**Candidates**:
1. **array-of-owned leak → caught? — probing** (FN if finder misses → check skipped).

## SafeFeatureFinder gating — RESOLVED (2026-06-08): gap mitigated by Sema-time net, no FN
The concern: SafeFeatureFinder (gates RequireBorrowCheck) detects owned/borrow via param/return
types, owned/borrow VARS (VisitVarDecl), borrow-ops (VisitUnaryOperator UO_AddrMut..AddrConstDeref),
casts — but NOT a CallExpr's owned RETURN type (VisitStmt recurses children, never VisitQualTypes the
call type). So a CALL-ONLY function (`f(){ mk(); }` discarding an owned return, no owned var) could
skip the dataflow borrow checker.
RESOLUTION: NOT an FN. Probed `_Safe void f(){ mk(); }` (mk returns owned, discarded) → CAUGHT
"memory leak because temporary variable 'mk()' is _Owned" — from the SEMA-TIME CheckTemporaryVarMemoryLeak
(SemaBSCOwnership:534), which runs independent of SafeFeatureFinder's borrow-checker gating. Two-layer
coverage: (1) Sema-time temp/move-leak checks catch leaks regardless; (2) dataflow borrow checker catches
CONFLICTS, which require borrow-taking/borrow-params that VisitUnaryOperator/VisitQualType DO detect.
The undetected cases (call-only, pointee-owned in _Unsafe) are either caught by layer 1 or non-bug-creating
or _Unsafe (borrow checker N/A). PROBED-inconclusive → RESOLVED-SOUND.

## BSCFeatureFinder::VisitQualType misses ensure_init ExtQuals — FOLDED into F59 (2026-06-22)
- VisitQualType (WalkerBSC.h:90-110) checks owned/borrow/trait/templatespec but NOT isEnsureInit()/isEnsureInitIfRet(); sole-ensure_init fn → verbatim rewrite path → `__attribute__((ensure_init))` leaks to .c (typedef path strips it). Same class as F59 (BSCFeatureFinder coverage gap → verbatim leak); LOW (tolerated, no hard break). NOT a separate filing; widens F59 (another missing marker in the same finder).

## BSCFeatureFinder missing nullability → F116 ROOT (2026-06-29)
`BSCFeatureFinder` (WalkerBSC.h:48) decides which decls `-rewrite-bsc` rewrites (RewriteBSC.cpp:509-521
`FindDeclsWithoutBSCFeature` → skip set; skipped decls emitted verbatim :654/911). It flags ONLY Owned/Borrow
(:91-92), Safe-zone (:125), owned-struct (:188), SafeStmt/SafeExpr (:268/270) — **NO `_Nullable`/`_Nonnull`
check**. So a nullability-ONLY decl (plain prototype `void f(int*_Nullable x)`, nullable-only struct field, global,
typedef) is classified "without BSC feature" → skipped → the clang-only nullability keyword reaches gcc → parse
error. This is the TRUE root of **F116** (broader than its documented "field type-spelling path"; F116's "params
strip" was masked by `_Safe` triggering the rewrite). Discriminator confirmed: `_Safe void f(int*_Nullable x)` STRIP;
`void f(int*_Nullable x)` KEEP+gcc-fail; `void f(int*_Owned o,int*_Nullable x)` STRIP. FIX: add `_Nullable`/`_Nonnull`
to BSCFeatureFinder. Repro: repro/F116b_rewrite_plain_prototype_nullable_breaks_gcc.cbs. Related: F59 (BSCFeatureFinder
VisitQualType owned/borrow), SafeFeatureFinder::VisitQualType (same missing-nullability shape, :398-399).
