# clang/lib/AST/BSC/TypeBSC.cpp — Function Notes

BSC-level type predicates and helpers — recursive type queries, qualifier
compatibility checks for redeclarations, move-semantics / trivial-data
classification. Same family of "only-outer-level" recursion bugs as F41
(`CheckOwnedFunctionPointerType`), F53/F56 (`HasDiffBorrorOrOwnedQualifiers` /
`HasDiffNullabilityQualifiers`) — but at distinct code sites with distinct
callers.

## AreOwnedBorrowQualifiersCompatible (TypeBSC.cpp:154-181) — PROBED-confirmed-F57

**Invariant**: For a SAFE/UNSAFE heterogeneous pointer-type redeclaration,
the SAFE side must not drop a BSC qualifier (`_Owned` / `_Borrow` /
`_ArrayElem`) present on the UNSAFE side, and `_Owned` ⟷ `_Borrow` may
never be mixed.

**Peers**:
- `HasDiffBorrorOrOwnedQualifiers` (SemaDeclBSC.cpp:85-101) — homogeneous
  redecl gate; F53 confirmed this only-outer-level recurses through
  PointerType but NOT FunctionProtoType.
- `HasDiffNullabilityQualifiers` (SemaDeclBSC.cpp:78-83) — same hole, F56.
- `AreParamTypesCompatible` lambda inside
  `areFunctionTypesCompatibleForHeterogeneousRedecl` (TypeBSC.cpp:228+) —
  this is the recursion caller for hetero-redecl param checks; need to
  read fully to determine whether it recurses into FunctionProtoType subtypes.
- `CheckOwnedFunctionPointerType` (SemaBSCOwnership.cpp:440-479) — F41
  exemplar of "predicate only checks outer level."

**Candidates**:
1. **Composition (most likely)** — the predicate's flag computation at lines
   156-165 is gated entirely on `isPointerType()`. If the input QualType is
   a `FunctionProtoType` whose parameter or return types carry BSC qualifiers
   (e.g. `void(int *_Owned)`), every `Is*` flag is false, the "drop" checks
   at lines 168-176 all skip, the owned/borrow mixing check at line 178 also
   skips, and the function returns `true` (compatible). So a SAFE/UNSAFE
   heterogeneous redecl of two function types whose param/return types
   differ in nested BSC qualifiers may pass this gate silently.
   Distinct from F53 (different file, different function, different
   redecl mode — heterogeneous vs homogeneous).
2. **Symmetry** — line 174-176 rejects "unsafe has owned/borrow without
   ArrayElem, safe has ArrayElem." The reverse (unsafe ArrayElem, safe
   non-ArrayElem) is NOT in the predicate. So `int *_Owned _ArrayElem`
   unsafe → `int *_Owned` safe would slip the predicate, even though the
   ArrayElem qualifier is being dropped. Same shape as the F53 family but
   for the ArrayElem dimension.
3. **Reachability** — the predicate has no nullability dimension at all
   (no `_Nullable` / `_Nonnull` checks). Heterogeneous redecl where the
   only difference is a nullability mismatch outer-level passes this gate.
   Sibling of F56 (which is at SemaDeclBSC.cpp:78); this site is at
   TypeBSC.cpp:154, called from a different caller chain.

Top candidate: **#1** (FunctionProtoType not handled). Probe: declare
function `_Safe` with fnptr param whose pointee is `_Owned`; redeclare
unsafe (no `_Safe`) with fnptr param whose pointee is raw. Expect: if the
gate has the C1 only-outer-level hole, the two are silently merged as
compatible.

Note: candidate #1 might be SHAPE-REJECTED if the safe/unsafe redecl
heterogeneity check fires before `AreOwnedBorrowQualifiersCompatible`
reaches the FnProto types — depends on call chain from
`areFunctionTypesCompatibleForHeterogeneousRedecl`. Worth probing.

## RecordType::hasOwnedFields vs hasBorrowFields (TypeBSC.cpp:416-481) — PROBED-BENIGN 2026-05-29
**Invariant**: BFS over record fields (deref pointer chains) detecting any directly/indirectly
owned- (resp. borrow-) qualified field. Used by IsTrackedType, safe-zone trivial-conversion,
&_Mut-of-struct, etc.
**Asymmetry (cosmetic, NOT a bug)**: hasOwnedFields canonicalizes FieldTy at the basic-case
check (:425 getCanonicalType); hasBorrowFields does NOT (:458 raw FD->getType()). BUT
`isBorrowQualified()`/`isOwnedQualified()` (Type.h:7047-7055) check `isLocal*Qualified() ||
CanonicalType.isLocal*Qualified()` — canonical-aware — so the missing upfront canonicalization
is masked. Verified: `typedef int *_Borrow B; struct S{B f;}` is detected IDENTICALLY to a direct
`int *_Borrow f` (both reject S*_Borrow→void*_Borrow "contains _Borrow even indirectly"); trivial
struct allowed. No detection gap. Not filed.

## areFunctionTypesCompatibleForHeterogeneousRedecl + AreParamTypesCompatible (TypeBSC.cpp:190-304) — UNPROBED [POST-584c8ae REWRITE]

**Context**: commit 584c8ae rewrote the heterogeneous `_Safe`/`_Unsafe` redecl
compat path (F57 was filed against the OLD code at :154-181). The NEW code adds
an `AreParamTypesCompatible` lambda (:235-286) that DOES now recurse into fnptr
params/returns (:259-279, via re-entrant `areFunctionTypesCompatibleForHeterogeneousRedecl`).
So the plain F57/F53 "fnptr-buried qualifier not recursed" hole is likely CLOSED
for the *directly-fnptr* param shape. Re-walk to find what the NEW recursion STILL misses.

**Invariant**: a heterogeneous (`_Safe` vs unsafe) redecl pair is compatible iff,
for each param + return, the SAFE side may only ADD `_Owned`/`_Borrow`/`_ArrayElem`
to a raw param/return, never REMOVE one, never SWAP `_Owned`↔`_Borrow`; return-type
C-quals preserved, param C-quals stripped — AT EVERY NESTING DEPTH.

**Callers**: SemaDecl.cpp:2559 (ptr-to-fnptr wrapper `areFunctionPointerTypesCompatible...`),
:4331 (MergeFunctionDecl C++/general path), :4404 (MergeFunctionDecl !CPlusPlus BSC path).
The heterogeneous branch at :4410 returns MergeCompatibleFunctionDecls and SKIPS the
later `HasDiffNullabilityParamsTypeAtBothFunction` (:4420) and the homogeneous owned/borrow
check (:4413) — so for hetero redecl, AreParamTypesCompatible is the ONLY BSC qual gate.

**The lambda's three arms** (per param/return pair UnsafeT,SafeT):
1. **Fast path** (:250-253): strip OUTER nullability+owned+borrow+arrayelem from both;
   if `canonical-unqualified` types now EQUAL → call `AreOwnedBorrowQualifiersCompatible`
   on the ORIGINAL (unstripped) types. That predicate (:154-181) inspects ONLY the
   OUTER pointer's owned/borrow/arrayelem flags (`isPointerType() && is*Qualified()`).
   ⇒ **nested-pointee qualifiers are NEVER compared in the fast path.** If
   `int *_Owned *` and `int **` canonicalize to the SAME unqualified type after outer
   strip, the inner `_Owned` is dropped silently. (Need to verify canonicalization.)
2. **Fnptr recursion** (:259-279): both fnptr → re-enter hetero check on pointees.
   Closes F57's direct-fnptr-param hole.
3. **General case** (:282-285): `typesAreCompatible` then outer `AreOwnedBorrowQualifiersCompatible`.

**Candidates**:
1. **Plain-pointer-NESTED owned/borrow drop (fast-path hole)** — `int *_Owned *p`
   (unsafe) vs `int **p` (safe): the OUTER pointer has no owned/borrow; the inner
   `_Owned` is one level deep. Fast path strips only outer → if canonical-unqualified
   matches → AreOwnedBorrowQualifiersCompatible sees outer-only → returns true →
   SAFE side silently drops the inner `_Owned`. DISTINCT from F57 (fnptr-buried, now
   recursed) and F76 (fnptr-PARAM-of-fnptr, assignment path). This is the REDECL path,
   PLAIN-pointer nesting. **Top candidate** — different nesting kind than any filed bug.
2. **Nullability dimension entirely absent in hetero redecl** — AreOwnedBorrowQualifiersCompatible
   has NO nullability check; the lambda strips nullability before compare; and the
   hetero branch (:4410) returns before the homogeneous `HasDiffNullabilityParamsTypeAtBothFunction`
   (:4420). So `_Safe void f(int *_Nonnull p)` vs `void f(int *_Nullable p)` (or the
   reverse) may merge silently. F56 is the HOMOGENEOUS nullability hole — hetero is a
   distinct caller/path. Need to check if nullability redecl mismatch is checked elsewhere.
3. **Return-type C-qual (const/volatile) preserve vs strip asymmetry** — comment at
   :151-152 says safe may only ADD quals; the lambda strips owned/borrow/arrayelem but
   relies on `typesAreCompatible` for cv. Per the rule, return-type C-quals must be
   PRESERVED but param C-quals STRIPPED. The lambda treats return and param IDENTICALLY
   (same AreParamTypesCompatible) — does it wrongly STRIP a return-type const? Probe:
   `const int *_Safe f(...)` ret vs raw-ret. (Lower priority — typesAreCompatible likely catches.)

Top: **#1** (plain-pointer-nested owned/borrow drop in fast path).

**RESULT (2026-05-30, bsc-explorer): CONFIRMED-new (candidate #1), pending Fxx.**
`void f(int *_Owned *p); _Safe void f(int **p);` ACCEPTED (drop), and the SWAP
`_Safe void f(int *_Borrow *p);` ACCEPTED — ast-dump shows the decls MERGE with
contradictory types. Homogeneous nesting REJECTED (Control 1) -> hetero-only hole.
Nested const REJECTED -> typesAreCompatible recurses C-cv; only BSC quals (stripped
:243-248) dropped. All dims (owned/borrow/arrayelem, param+return) leak. Runtime
double-free witness (Invalid free, 1 alloc/2 frees). Distinct from F41/F53/F57/F76.
Repro `/tmp/explorer_hetero_nested_qual_redecl.cbs`, runtime `/tmp/explorer_rt2.hRxh7F.cbs`.

## RecordType::hasOwnedFields / hasBorrowFields (TypeBSC.cpp:423-488) — probing array-element gap
**Invariant**: returns true iff the record transitively contains an `_Owned`
(resp. `_Borrow`) field — directly, through pointer pointees (:438 loop), or
through nested record fields (:446 BFS). Used by the "array cannot be qualified
by _Owned (even indirectly)" Sema gate, ownership tracking, and BSCFeatureFinder.
**Peers**: `isOwnedStructureType` (basic-case :433), `PointerType::hasOwnedFields`
(:57), `withBorrowFields` (:490). F57 (AreOwnedBorrowQualifiersCompatible) sibling.
**Candidates**:
1. **ARRAY-of-struct field not unwrapped — probing**. The BFS only descends via
   `FieldTy->getAs<RecordType>()` (:446), which is NULL for an ArrayType. A field
   `struct Inner arr[N]` (Inner has an `_Owned` field) → array is not a record,
   not a pointer, not owned-qualified → MISSED. If `struct Outer{struct Inner
   arr[N];}` is then ACCEPTED + Outer classed non-owned → inner owned ptrs
   untracked → silent leak. HIGH if real.
2. **pointer loop `isOwnedQualified() && !isOwnedStructureType()` (:441)** asymmetry
   vs basic case (:433 has no `&&!isOwnedStructure`). UNPROBED.
3. **hasBorrowFields same array-element gap (:481)** — but borrow leaks are FP-only. UNPROBED.

## AreParamTypesCompatible (lambda in areFunctionTypesCompatibleForHeterogeneousRedecl, :235-286) — read, gaps FILED
INVARIANT: for a heterogeneous safe/unsafe redecl, each param/return type is compatible iff —
after stripping outer nullability + owned/borrow/arrayelem (:241-248) — base types match (canonical
unqualified, :251) AND AreOwnedBorrowQualifiersCompatible (:253/:285, =F57 filed). Safe/unsafe fnptr
PARAMS recurse (:259-278, areFunctionTypesCompatibleForHeterogeneousRedecl). NULLABILITY is stripped
here and checked separately by HasDiffNullabilityQualifiers (=F56 filed, FnProto-recursion gap).
No NEW gap — the recursion-incompleteness instances in this family are F56 (nullability) + F57 (owned/borrow),
both filed; this site delegates to them. Behaviorally consistent (het.cbs heterogeneous redecl sound).

## hasOwnedRetOrParams (:109) vs hasBorrowRetOrParams (:121) — early-out granularity asymmetry (LOW, noted)
hasOwnedRetOrParams uses `isOwnedQualified()` (TOP-LEVEL owned on ret/params); hasBorrowRetOrParams uses
`hasBorrow()` (broader/indirect). Both gate the owned/borrow fnptr-compat early-out (SemaBSCOwnership:462/:883
— "if neither side has owned/borrow ret/params, skip the check"). ASYMMETRY noted but likely BENIGN: the owned
fnptr-compat check only concerns top-level owned qualifiers (caught by isOwnedQualified); struct-owned-field
params have no top-level qualifier to mismatch (struct type fixed); inner-owned `int *_Owned *` has a raw outer
pointer (_Unsafe edge). CONFIRMED BENIGN 2026-06-08: read QualType::hasBorrow (:542 = isBorrowQualified OR hasBorrowFields).
The owned fnptr-compat check (CheckOwnedFunctionPointerType) only concerns TOP-LEVEL owned qualifiers,
so the narrower isOwnedQualified early-out correctly gates it (struct-owned-field param has no top-level
qualifier to mismatch). hasBorrow's breadth is over-cautious/lifetime-related, not a soundness requirement
for owned. NOT a gap.

## isMoveSemanticType (:341) — read + probed, SOUND
Recursive owned-containment check: owned-qualified → true; RecordType → Visited-guarded field iteration
(direct owned field → true; nested-struct field → recurse). Pointer/array fields don't make a type
move-semantic (correct — ownership is direct). Visited SHARED across siblings is SAFE: a move-semantic
type returns true at its FIRST occurrence (before the Visited guard could false-negative a same-type
sibling); non-move-semantic types are consistently so. Probed: `struct Outer{struct M m1,m2;}` (M owns)
freeing only m1.p → m2.p leak caught → both same-type fields tracked. No gap.

## getVariableArrayDecayedType — Type::Conditional in llvm_unreachable block (ASTContext.cpp:3632) — reading 2026-06-17
INVARIANT: a `ConditionalType` is assumed to NEVER be variably-modified; if decayed while VLA-containing → `llvm_unreachable("type should never be variably-modified")`. BUT the ConditionalType ctor (TypeBSC.cpp:521) ORs in `T1->getDependence() | T2->getDependence()`, so a VLA branch makes the conditional VariablyModified → invariant violatable.
Candidates:
1. [reachability] `conditional<C, int[n], int>` (VLA branch, n runtime) as a fn PARAM (params decay variably-modified types) → getVariableArrayDecayedType(conditional) → unreachable CRASH. **UNPROBED** (top)
2. [composition] same conditional as a local with sizeof/decay → maybe triggers. UNPROBED
3. [symmetry] Trait type in the same unreachable block — traits can't easily carry VLAs; low. UNPROBED

## const-generic value → array bound (size computation) — reading 2026-06-17
INVARIANT: a constant-generic argument N used as `T d[N]` must produce a valid ConstantArrayType; pathological N (overflow of N*sizeof(T), INT_MIN, multi-dim overflow) must be diagnosed, not crash.
Candidates:
1. [reachability] N huge so N*sizeof(elem) overflows size_t (e.g. char[N], N≈2^62) → array-size overflow crash? **UNPROBED** (top)
2. [composition] multi-dim `T d[N][N]` with N≈2^32 → product overflow. UNPROBED
3. [boundary] N = INT_MIN / LONG_MIN via const-generic. UNPROBED

## ConditionalType codegen for _Owned-resolved type (runtime) — reading 2026-06-17
INVARIANT: a value whose declared type is `conditional<...>` resolving to `T *_Owned` must be allocated/freed exactly once at runtime (codegen sees through the conditional). G01 showed nullability SUGAR is lost via desugar→canonical; _Owned is a QUALIFIER (survives canonical) so leak-check works at compile — but does CODEGEN emit the right free?
Candidates:
1. [reachability] alloc+free an owned value through a conditional-typed local; valgrind for leak/double-free. **UNPROBED** (top)
2. [composition] conditional owned in a generic fn, two instantiations. UNPROBED

## generic type deduction × ownership/borrow qualifiers — reading 2026-06-17
INVARIANT: deducing T from an argument must preserve ownership/borrow/const so the instantiated body's move/borrow analysis is correct; a mis-deduced qualifier could cause a use-after-move/leak FN.
Candidates:
1. [reachability] deduce T from an _Owned-by-value arg, body moves T → is the move tracked? (vs explicit). **UNPROBED** (top)
2. [composition] deduce through `T *_Borrow` param from &_Mut x; body writes *p — borrow region correct? UNPROBED
3. [boundary] deduce T where two args give conflicting qualifiers (const vs non-const). UNPROBED

## Plain generic type-alias `Owned<T> = T *_Owned` → ownership-tracker survival (substitution path) — PROBED-SOUND 2026-06-23 (bin 34e6f26e)

**RESULT**: SATURATED-SOUND. 9 probes across all qualifiers × all analyzer paths; every alias-substituted type is treated identically to the direct form. Distinct from G10 (array-CHECK bypass), G01 (conditional desugar sugar-strip), G12 (generic-fn typearg sugar-strip) — here the qualifier itself survives substitution. See `_probed.md` 2026-06-23 cycle for the 9 shapes. Recommend widening OFF this surface (next: generic-TYPE-ALIAS mangle path vs G14's struct-record mangle).

**INVARIANT**: a PLAIN (non-conditional, non-array) generic type alias
`typedef Owned<T> = T *_Owned;` instantiated as `Owned<int>` must resolve to a type
that the ownership analyzer treats IDENTICALLY to the direct `int *_Owned` form —
i.e. a local `Owned<int> p = safe_malloc<int>(1)` left unfreed must fire
`memory leak of value: p` at scope exit (compile) AND valgrind `definitely lost`
at runtime, exactly as the direct form does. The qualifier must survive template
substitution into the analyzer's tracked-type / leak-check path.

**CONTEXT (dedup)**:
- G10 (`Arr<T>=T[N]; Arr<int*_Owned>`) = the ARRAY path: the owned-array restriction
  check `CheckOwnedOrIndirectOwnedType(...,"array")` (SemaType.cpp:5202) is
  DECLARATOR-path-only and is NOT re-invoked on the generic-alias substitution →
  forbidden owned-array type admitted → leak FN. G10's blast-radius probes br1/br2/br3
  ONLY tested GLOBAL decls of `Id<T>`/`Ptr<T>`/owned-global (all REJECTED — those
  gates see alias-substituted types); they did NOT test a LOCAL scalar owned var
  through a plain `Owned<T>=T*_Owned` alias for LEAK-TRACKING parity.
- G01 (`conditional<C,T,F>`) = the CONDITIONAL path: `desugar()`→`getCanonicalType`
  strips nullability AttributedType SUGAR; `_Owned` QUALIFIER survives (co1 etc.).
- G12 = generic-FUNCTION type-template-arg strips nullability sugar on substitution.
- co1 tested conditional→_Owned survives; the PLAIN generic-alias→_Owned (scalar local)
  leak-tracking parity has NOT been explicitly probed. The hypothesis: if the
  generic-alias substitution builds the local's type on a path that bypasses the
  ownership-tracker's `IsTrackedType`/leak registration the SAME way G10 bypasses the
  array check, a local `Owned<int> p = safe_malloc` could leak silently.

**PEERS**:
- `IsTrackedType` (BSCOwnership.cpp) — gates whether a local's owned type is tracked at all.
- `VisitDeclStmt` pointer-owned init branch (BSCOwnership.cpp:2357-2364) — registers
  `setToOwned(VD)` / leak-check; reads the local's QUALTYPE.
- direct `int *_Owned p = safe_malloc<int>(1)` (baseline) — known to fire leak on no-free.
- non-generic `typedef int *_Owned OPtr; OPtr p = safe_malloc;` (control) — known sound
  (typedef qualifier preservation probed td.cbs/td2.cbs line 4573).

**CANDIDATES** (ranked):
1. **[symmetry] leak-tracking parity for plain generic alias** — `typedef Owned<T>=T*_Owned;`
   `Owned<int> p = safe_malloc<int>(1);` (no free) → does it fire `memory leak of value: p`?
   If NOT (and valgrind definitely-lost) while the direct form DOES → the generic-alias
   substitution path drops the local from the ownership tracker (sibling of G10's array
   path, but on the LOCAL-SCALAR leak-tracking side, distinct fix surface). **UNPROBED (top).**
2. **[symmetry] borrow-rule parity for `Ref<T>=T*_Borrow`** — `typedef Ref<T>=T*_Borrow;`
   then a double-mut-borrow / use-after-scope through `Ref<int>`: enforced like direct `_Borrow`?
3. **[symmetry] nullability parity for `NN<T>=T*_Nonnull`** — G01's note claims this is
   "correctly rejected"; re-confirm on current bin as a control (not the bug, just parity).
4. **[composition] alias nested in a borrow/owned field** — `struct S{ Owned<int> f; }` field
   leak-tracking (cf. G10 struct-field extension).

## addConstBorrow / removeConstForBorrow (TypeBSC.cpp:567-600) — read+probed 2026-06-17, PROBED-SOUND

**Invariant**: `addConstBorrow` builds the result of `&_Const E` — pointee = E's type (pointer:
its pointee; value: E itself), `addConst()` the pointee, `getPointerType` + `addBorrow`. The const
is genuinely applied to the borrowed object (so the result borrow is immutable). `removeConstForBorrow`
(dereffing a `const T *_Borrow`) removes the pointee's local const, preserving owned/borrow quals.

**Peers**: `isConstBorrow` (:548, pointee-const = the const-ness of a borrow); `CheckBorrowQualTypeCStyleCast`
(:605, the cast gate that rejects drop-const); the `&_Const`/`&_Mut` build path in SemaExpr.

**Candidates (probed)**:
1. (composition) `&_Const` result does NOT re-add `_ArrayElem` — `&_Const *p` of a `_ArrayElem` borrow
   yields a single-element const borrow w/o `_ArrayElem`. PROBED-SOUND/conservative-safe: drops the
   pointer-arithmetic capability (a restriction, not unsoundness); const IS enforced on the result
   (`*c = 99` → "read-only variable is not assignable"). probes acb1-acb3.
2. (symmetry) `removeConstForBorrow` also doesn't re-add `_ArrayElem` (:594-598 preserve owned/borrow only)
   — same conservative-safe direction. SOUND by reading.
3. (reachability) non-pointer operand path (`&_Const *a` where *a is a value type) — pointee=*this, const
   added; printing-faithful (comment :574). SOUND.

## removeConstForBorrow two-level intermediate-const strip — LOW (unsafe-only), NOT filed (2026-06-22)
- Earlier note marked addConstBorrow/removeConstForBorrow PROBED-SOUND for the ONE-level `const T *_Borrow` case (`*b=99` correctly rejected). GAP found (GLM-5.2 explorer): the TWO-level case `const T * *_Borrow` — `removeConstForBorrow` (:585-600) strips the const off the INTERMEDIATE pointee, so `int *a=*b; *a=99;` in UNSAFE code compiles warning-free (vs plain C / no-_Borrow which warn discards-qualifiers). BUT _Safe rejects both the deref and the const→nonconst conversion, so no safe-zone soundness hole; no new UB. LOW (unsafe-zone diagnostic regression), NOT filed.

## isMoveSemanticTypeImpl array-field recursion gap (TypeBSC.cpp:362-388) — probe 2026-06-24
**Invariant**: a type is move-semantic iff owned-qualified OR (record with a move-semantic field). Must
recurse ALL field types incl. ARRAY-of-move-semantic-struct.
**GAP**: the field loop only recurses `isa<RecordType>(FQT)` (:381) — an ARRAY field `struct Inner arr[N]`
(ArrayType, not RecordType) is NOT recursed → a struct with an array-of-(owned-containing-struct) field is
mis-classified NON-move-semantic. ASYMMETRY: sibling isTrivialDataTypeImpl (:401) DOES recurse arrays.
**Peers**: isTrivialDataTypeImpl (recurses arrays), IsTrackedType (F64), checkMemoryLeak.
**Candidates**: 1. **`struct Outer{ struct Inner arr[2]; }` (Inner has _Owned field) mis-classified non-move →
owned elems leak silently (FN)** UNPROBED ⭐. 2. nested array-of-array. 3. array field + scalar owned field (scalar saves it).

## areFunctionTypesCompatibleForHeterogeneousRedecl nullability-strip (TypeBSC.cpp:256-307) — probe 2026-06-24
**Invariant**: a _Safe/_Unsafe redecl pair must have compatible param types; owned/borrow checked via
AreOwnedBorrowQualifiersCompatible. **GAP**: NULLABILITY is STRIPPED (:262-263 stripOuterNullability) and
NOT re-checked → a pair differing only in param nullability is accepted.
**FN risk**: _Safe decl `_Nullable` param + _Unsafe DEF `_Nonnull` param → caller passes null via the safe
nullable decl → reaches the nonnull-assuming def → null deref. (Site-distinct from F29 fnptr-assign variance.)
**Peers**: F29 (fnptr nullability variance), F104/F105 (hetero-redecl ensure_init/owned), AreOwnedBorrowQualifiersCompatible.
**Candidates**: 1. **nullability-variance hetero-redecl null reaches nonnull def (FN) vs rejected** UNPROBED ⭐.

## AreOwnedBorrowQualifiersCompatible (AST/BSC/TypeBSC.cpp:154) — heterogeneous-redecl qual-compat (2026-06-27)
INVARIANT: a SAFE redecl must not DROP an owned/borrow/_ArrayElem qualifier present in the UNSAFE decl; owned↔borrow always
incompatible; recurses one pointer layer, and for fnptr pointees recurses into return+params (matched param counts) — so a
BURIED qualifier isn't dropped. THIS is F77's fix (hetero-redecl nested qual-drop now diagnosed). PEERS: DoPointerTypes
SatisfyAssignmentConstraintsImpl (the ASSIGNMENT path = F76, open, does NOT mirror this recursion → nested-fnptr-param
variance launders at assignment). CANDIDATES: 1. F76 (assignment path unmirror'd, open/filed). 2. param-count-mismatch edge
(falls to non-fnptr recurse) — niche redecl case, UNPROBED. 3. redecl recursion sound (F77 fixed).

## checkFunctionProtoType (AST/BSC/TypeBSC.cpp:133) — safe-zone spec on function types (2026-06-27)
INVARIANT: a function/fnptr type carries its SafeZoneSpecifier in FunctionProtoType::ExtProtoInfo.SafeZoneSpec;
checkFunctionProtoType(SZS) returns EPI.SafeZoneSpec==SZS. So _Safe/_Unsafe is PART OF the function type → preserved through
typedefs/fnptrs → a _Safe context can't call an _Unsafe fn even via a typedef'd fnptr. PEERS: the call-site safe-zone check,
IsUnsafeType. PROBED-sound: _Unsafe fn via typedef'd fnptr in _Safe → "_Unsafe function call is forbidden in the safe zone".

## AreParamTypesCompatible (TypeBSC.cpp:256, het-redecl param compat) — read 2026-06-29
**Invariant**: decide if an unsafe-decl param type and safe-decl param type are compatible for heterogeneous redecl.
STRIPS nullability (:262-263 stripOuterNullability) + owned/borrow/arrayelem (:264-269) for BASE-type compat, then
the fast path (:272-274) and general case (:303-306) return `AreOwnedBorrowQualifiersCompatible(orig,orig)` — checks
OWNED/BORROW only, **NEVER re-checks nullability** → het-redecl nullability drop/swap accepted = **F103 root**. Fnptr
recursion (:280-300). **Peers**: areFunctionTypesCompatibleForHeterogeneousRedecl, AreOwnedBorrowQualifiersCompatible
(F57/F77/F105 root), CheckNullabilityQualTypeAssignment (F66, assignment not redecl). **Candidates**:
1. (nullability-strip never re-checked = F103, FILED) outer + fnptr-nested + return-position.
2. (owned/borrow via AreOwnedBorrowQualifiersCompatible = F57/F77/F105 family).
3. (const/volatile via typesAreCompatible general path) — standard C, likely OOS.

## RecordType::hasBorrowFields / hasOwnedFields (TypeBSC.cpp:444,477) + Type::has*Fields (:72,92)
- **Invariant**: must report true iff the type contains, at any depth, an owned/borrow field — so the placement gate rejects no-lifetime storage of such types.
- **Peers**: CheckBorrowOrIndirectBorrowType / CheckOwnedOrIndirectOwnedType (the gates, SemaDecl.cpp:8556-8557 var, 18723-18724 union, SemaType.cpp:5202 array — F81 is the missing borrow call at the array site).
- **CONFIRMED-F124 (HIGH)**: RecordType::hasBorrowFields BFS enqueues a field only when `FieldTy->getAs<RecordType>()` succeeds (:502). An ARRAY-typed field is ArrayType, not RecordType → element never enqueued → borrow missed. Type::hasBorrowFields (:92) has no ArrayType case either. A global struct wrapping array-of-borrow-field-struct bypasses the var-site gate. Owned twin REJECTED (hasOwnedFields path / isOwnedStructureType handles it) → borrow-specific. **Fix**: unwrap array-typed fields (getAsArrayTypeUnsafe()->getElementType()) before the RecordType enqueue, or add ArrayType case to Type::hasBorrowFields (subsumes F81 too).
- Candidates: (1) UNPROBED — does hasBorrowFields recurse through a POINTER-to-array-of-borrow field? (2) UNPROBED — `withBorrowFields`/`withBorrowFieldsImpl` (:101) — does that variant also miss arrays? (3) PROBED-F124 — array-typed direct field.
