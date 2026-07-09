# clang/lib/Sema/BSC/SemaBSCSafeZone.cpp — Function Notes

F51 already filed at `IsSafeConversion` enum-to-int sub-block (lines 943-957). This file's other restriction predicates remain partly read.

## IsSafePointerConversion (SemaBSCSafeZone.cpp:819-851) — read 2026-06-17

**Invariant**: a safe-zone pointer→pointer conversion is allowed only if it
PRESERVES the pointee type modulo three explicitly-permitted relaxations —
(1) borrow→borrow may DROP `_ArrayElem` (`T*borrow ← T*borrow _ArrayElem`);
(2) borrow→borrow may ADD const (`const T*borrow ← T*borrow`); (3) borrow→borrow
to `void*` allowed when pointee is trivial AND const-ness matches; plus owned→void
(`void*owned ← T*owned`). Everything else falls to `SrcCanPtr == DstCanPtr`.
Crucially: `IsExplicitCast` is NOT honored here — pointer type-punning is forbidden
even with a C-style cast (unlike the arithmetic branch). Dropping const and ADDING
`_ArrayElem` are both forbidden (the conservative/sound direction).

**Peers**: `IsSafeConversion` (sole caller, 2 sites: ptr-ptr + array-decay-ptr);
nullability gate `CheckNullabilityQualTypeAssignment` (SemaDeclBSC, F66) — IsSafe-
PointerConversion compares POINTEES only, never the pointer-level `_Nonnull`/
`_Nullable`, so nullability soundness rests ENTIRELY on that separate gate (layered
defense). `IsSafeFunctionPointerTypeCast` (fnptr peer, separate).

**Candidates (ranked)**:
1. (composition) **Nullability laundering via ptr-ptr conversion** — IsSafePointer-
   Conversion approves `_Nonnull T* ← _Nullable T*` (pointees equal) and does NOT
   look at pointer nullability; the ONLY thing rejecting it is the separate
   nullability gate. If that gate misses the safe-zone init/cast path → FN
   (`_Nullable` laundered to `_Nonnull`). TOP — probed below.
2. (symmetry) **`removeLocalConst` is top-level only** — deep const (`const int**`
   vs `int**`) relies on full QualType `==` in the fallback. Verified by reading:
   the deeper const difference makes `SrcCanPtr != DstCanPtr` → rejected. Likely SOUND.
3. (reachability) **owned→void has no const/trivial guard** — `void*owned ←
   const T*owned` allowed unconditionally; but const-owned is not a meaningful
   reachable construct (owned = heap ownership), low value. UNPROBED-low.

## DiagnoseInvalidUnaryExprInSafeZone UO_AddrOf (SemaBSCSafeZone.cpp:1237-1241) — read 2026-06-17, PROBED-SOUND

**Invariant**: in `_Safe`, raw `&` is forbidden unless the operand is a FUNCTION type
(`&func`→fnptr OK). Type-based check on the operand type.
**Form-completeness**: address-of is ALWAYS UnaryOperator UO_AddrOf (no alternate AST path),
so unlike the UO_Deref guard (F68: ArraySubscript `q[i]` bypasses it), there is NO form-bypass.
Confirmed: `&x`/`&arr[0]`/`&s.f`/`&*pb` ALL rejected "'&' operator is forbidden". SOUND.
**Peer**: UO_Deref guard (:1243) — has the F68 subscript bypass (re-validated LIVE 2026-06-17).

## CheckCStyleCast BSC block (SemaCast.cpp:2848-2934) — read 2026-06-17 (peer of IsSafeConversion)

**Invariant**: a C-style cast in BSC must pass IsSafeConversion(IsExplicitCast=true)
+ IsSafeFunctionPointerTypeCast + owned/borrow CStyleCast checks (lines 2911-2933).
TWO early returns bypass the whole BSC block: (a) **cast-to-void** (:2861, returns
`CK_ToVoid` before the BSC `#if` at :2872) — harmless, void discards; (b)
**type-dependent** cast in a generic body (:2874-2879, `assert(Kind==CK_Dependent)`)
— DEFERRED, must be re-checked at instantiation (else SILENCE-not-DEFER = §G FN).

**Peers**: IsSafeConversion (the gate, shared with assignment site SemaExpr:10795);
the instantiation machinery (Sema template instantiation re-runs CheckCStyleCast on
the substituted type) — the deferral's soundness depends ENTIRELY on that re-run.

**Candidates (ranked)**:
1. (composition/§G) **deferred forbidden cast not re-checked at instantiation** —
   `_Safe int bad<T>(T t){ return (int)t; }` instantiated `bad<double>` makes
   `(int)t` an explicit float→int (forbidden in _Safe EVEN with cast). If the
   instantiated body compiles clean → SILENCE FN (the §G hypothesis, cast path). TOP.
2. (reachability) **cast-to-void skips owned/borrow CStyleCast checks** — `(void)owned`
   skips CheckOwnedQualTypeCStyleCast; but counts as a use + void discards (no value
   recovered), likely harmless. UNPROBED-low.
3. (symmetry) **nullptr→bool/ptr implicit-cast insertion** (:2891-2902) before the
   IsSafeConversion gate — ordering looks fine (gate runs after). Low.

## CanBeUninitializedInSafeZone (SemaBSCSafeZone.cpp:1131-1175) — PROBED-SOUND (Chain R, 2026-05-30): decl-gate laxity fully backstopped by use-time init analysis; FN angle = F01/F07 (filed LOW)

**Invariant (documented, bsc-safe-zone §3)**: decides which types MAY be left
uninit in a `_Safe` zone. Basic types yes; POINTER types (raw/_Owned/_Borrow/
fnptr) MUST init; struct/union WITH a pointer field needs COMPLETE init; struct/
union WITHOUT pointer fields yes.

**What the code ACTUALLY does**: ONLY rejects `isOwnedStructureType()` — at top
level (:1137) and recursively over struct/union fields + array elements (:1153).
EVERYTHING else returns `true` (can be uninit), incl. raw/_Owned/_Borrow POINTERS
at top level and structs WITH raw/_Owned/_Borrow pointer fields. So the decl-time
gate is FAR laxer than the doc — it is essentially "owned-struct-by-value only".

**Caller**: ParseDecl.cpp:2630 (`InitKind::Uninitialized` branch) — fires
`err_unsafe_action "uninitialized declarator"` iff `!CanBeUninitializedInSafeZone`.

**Reconciliation with F01/F07 (already filed LOW)**: F07 established raw/_Owned/
_Borrow uninit DECL is intentionally accepted; misuse caught at USE (raw deref
forbidden; `use of uninitialized value` dataflow diag; leak check). So decl-gate
laxity alone is NOT a new bug. The Chain-R target = a type where NEITHER the
decl-gate NOR the use-time check fires → genuine uninit-USE FN.

**Peers**: init analysis `BSCIRInitAnalysis` (the use-of-uninit dataflow, gated
on `-uninit-check`), `DiagnoseInvalidUnaryExprInSafeZone` (F68, raw deref guard),
ownership leak check.

**Candidates**:
1. struct/union WITH a raw-pointer field, uninit, then read the pointer field and
   use it — does init analysis cover the pointer FIELD read, or only owned fields?
   (mirrors F77/F79/F80 nesting-too-shallow). Top.
2. `_Borrow` pointer field of a plain struct, uninit, then read — borrow use-check
   vs init dataflow coverage.
3. array-of-pointers element read after uninit decl.

## DiagnoseInvalidUnaryExprInSafeZone (SemaBSCSafeZone.cpp:1175-1225) — PROBED-confirmed-F68

**Invariant**: inside a safe zone, raw-pointer operations that can produce UB
(`*` deref, `&` addr-of, `++`/`--` arith) on non-owned / non-borrow pointers
are rejected. The `*` arm (case UO_Deref, line 1207) fires for raw pointers
(allowing fnptr deref and string-literal deref).

**Peers**: `DiagnoseInvalidMemberAccessExprInSafeZone` (line 1146, `->`/`.`
arms). Both are **UnaryOperator/MemberExpr** guards wired in from
SemaExpr.cpp:16839 and SemaExprMember.cpp:2029 respectively.

**F68 (HIGH, filed)**: this guard is UnaryOperator-only. `ArraySubscriptExpr`
(`q[i]` = `*(q+i)`, a raw deref) is a separate AST node on the
ActOnArraySubscriptExpr build path, which has **no** safe-zone deref hook —
grep `SafeZone`/`Subscript` in this file = zero ArraySubscript handling. So
`q[i]` on a raw `int *_Nonnull` compiles clean in `_Safe` while `*q` and
`*(q+0)` are rejected → arbitrary OOB read/write from safe code (valgrind
Invalid write+read). `repro/F68_safe_zone_array_subscript_raw_deref.cbs`.
C1 wrapper/AST-kind-missing; distinct check from F36 (mut-borrow string-lit)
and F39 (borrow-checker DefUse). Fix: add the raw-deref check on the subscript
build path.

**Sibling-arm blast radius (probed, NOT filed)**: the `++`/`--` arm
(lines 1183-1199) forbids raw-pointer `q++`/`q--` but the compound-assign
forms `q += 1` / `q -= 2` (BinaryOperator BO_AddAssign, a different node)
bypass it — accepted in `_Safe`. SHAPE-REJECTED as a soundness defect: per
bsc-safe-zone docs, raw pointer *arithmetic* is not forbidden (only raw
*dereference*, `&`, and casts are). `q += 1` derefs nothing; the result is
only exploitable via `q[i]` = F68. So this is not an independent soundness
hole — the `q++` prohibition is a stylistic restriction, not a memory-safety
boundary. See `_probed.md` 2026-05-29 (cont.).

## GetSafeArrayDecayType (SemaBSCSafeZone.cpp:835-839) + Sema::isBorrowArrayDecayTypeMatch (SemaExpr.cpp:545-576) — PROBED-SOUND 2026-05-30 (Chain I)

**Invariant**: an array `T[N]` may decay to a `_Borrow`/`_Borrow _ArrayElem`
destination in `_Safe` only when the element type matches the dest pointee
(modulo ADDING const/volatile/restrict and the trivial-data→void rule); it
must NOT launder by DROPPING a pointee qualifier or changing the element type.

**Chain structure (the key finding)**: when `isBorrowArrayDecayTypeMatch`
returns TRUE, `GetSafeArrayDecayType` returns `DestPtrType` **verbatim**, so the
subsequent `IsSafePointerConversion(SrcDecayedCanType, DestCanType)` at
IsSafeConversion :891-894 compares `DestType` against `DestType` — a TAUTOLOGY
(always true). So **all soundness of the array-decay-to-borrow path rests on
`isBorrowArrayDecayTypeMatch` ALONE**; the `IsSafePointerConversion` hop is dead
for the matched case. (When the match returns FALSE, `getArrayDecayedType` gives
the plain `T*` and `IsSafePointerConversion` does the real raw-decay check.)

**Peers**: `IsSafePointerConversion` (:801, the pointer→pointer matrix — audited
SOUND earlier this session); `MaybeDecayArrayToBorrowArrayElemPointer`
(SemaExpr.cpp:578-633, the EXPR-building counterpart, runs regardless of safe
zone). `isTrivialDataType` (TypeBSC.cpp:411 — pointer field/owned/borrow field
makes a struct non-trivial; correctly excludes struct-with-raw-pointer arrays
from the void-borrow rule).

**Decay matrix probed (all SOUND or SHAPE-REJECTED), 2026-05-30:**
| src array | dest | result | note |
|-----------|------|--------|------|
| const int[N] | int*_Borrow | REJECT | drop const blocked (line 559 strip only ADDs const) |
| const int[N] | int*_Borrow _ArrayElem | REJECT | drop const into mutable elem blocked |
| const int param (=const int*) | int*_Borrow _ArrayElem | REJECT | goes via pointer path, blocked |
| int[N] | long*_Borrow | REJECT | element-type mismatch (hasSameType false) |
| int[N] | void*_Borrow | ACCEPT | trivial→void (correct) |
| struct{int*}[N] | void*_Borrow | REJECT | non-trivial (raw ptr field) blocked |
| const int[N] | void*_Borrow | REJECT | line 575: const elem, non-const void → false |
| const int[N] | const void*_Borrow | ACCEPT | correct |
| int[N] | const void*_Borrow | ACCEPT | add const to void (correct) |
| volatile int[N] | int*_Borrow | REJECT | drop volatile blocked |
| int[N] | volatile int*_Borrow | REJECT(other) | "incompatible _Borrow types" fires FIRST — array form's volatile-ADD path (lines 561-564) is effectively dead under safe-zone borrow-compat check |
| int[N] | int*_Borrow _ArrayElem _Nonnull | ACCEPT | add nonnull (array always non-null) |
| char[N] | const char*_Borrow _ArrayElem | ACCEPT | add const on elem |
| int[2][3] | int(*_Borrow _ArrayElem)[3] | ACCEPT | matching pointer-to-array |
| int[2][3] | void*_Borrow | ACCEPT | trivial elem |
| int[2][3] | int*_Borrow _ArrayElem | REJECT | dimension-skip blocked |
| int*_Owned a[N] | (any) | SHAPE-REJECT | "type of array cannot be qualified by _Owned" |
| int[N] | raw int* (safe) | ACCEPT | no qual change; deref still forbidden in safe |
| const int[N] | raw int* (safe) | REJECT | const drop via raw decay caught by IsSafePointerConversion fallback |
| int[N] | int*_Borrow then p[2] | REJECT | plain (no _ArrayElem) borrow forbids subscript |

**NON-decay note (folds into existing borrow-checker behavior, NOT this chain):**
two mutable `int*_Borrow _ArrayElem` borrows of the SAME local array compile
clean — BUT the explicit `&_Mut a[0]` twice form is ALSO clean, so this is an
`_ArrayElem` exclusivity property of the borrow checker (F39 family), NOT a
decay-type defect. The decay path produces the same AST (`&_Mut a[0]`) as the
explicit form (MaybeDecayArrayToBorrowArrayElemPointer :604-632), so it can't
diverge.

**VERDICT**: Chain I decay TYPE matrix is SOUND. No qualifier launder, no
element-type confusion, no missing trivial-data exclusion. The `_ArrayElem`
requirement, const/volatile/restrict, void, nullability, multi-dim, and
struct-with-pointer dimensions all match the pointer→pointer form (or are
shape-rejected upstream). Distinct surface from F51 (builtin narrowing) and the
IsSafePointerConversion audit. Chain I → SATURATED @ 28656aa9.

## Chain P — DoPointerTypesSatisfyAssignmentConstraints{,Strict} caller table (SemaBSCSafeZone.cpp:395-504) — TRACED 2026-05-30

**Invariant**: a context that needs strict pointer-assignment compatibility (no
implicit conversion, const-drop rejected, exact pointee) must call the *Strict
wrapper; a context where the laxer C-implicit-conversion rules are sound (the
normal Clang arg-conversion machinery runs AFTERWARD) may call the non-strict
wrapper.

**Caller table (tree-wide, exhaustive):**
| wrapper | caller | context | AllowImplicitConversions |
|---------|--------|---------|--------------------------|
| non-strict `DoPointerTypesSatisfyAssignmentConstraints` | `CheckCallAssignmentConstraints` (SemaBSCSafeZone.cpp:335) | heterogeneous-redecl SELECTION gate for a *function CALL* — picks which _Safe/_Unsafe redecl binds to the actual args | true |
| strict `...Strict` | `BorrowParamTypesMatch` lambda in `CheckBorrowFunctionPointerType` (SemaBSCOwnership.cpp:891) | fnptr-assignment borrow-variance, per param/return | false |
| Impl direct (false) | `DoesFunctionPointerSatisfyConstraints` (:515,:527) | fnptr-assignment per param/return | false |

**What strict adds over non-strict (Impl :454-491):**
- non-strict pointee check (:470): `typesAreCompatible(DestPointee.getUnqualifiedType, SrcPointee.getUnqualifiedType)` — strips ALL quals (incl const) and "lets Clang handle const checking later". void* either direction OK.
- strict pointee check (:482-489): exact canonical-unqualified match + explicit `const→mut` REJECT (:488). No void* relaxation.

**The non-strict caller `CheckCallAssignmentConstraints` only fires for HETEROGENEOUS-redecl functions** (`SelectDeclForHeterogeneousRedecl` returns early at :258 unless the fn has BOTH a _Safe and _Unsafe decl). So the non-strict gate is the BSC-qualifier check that decides which redecl binds; after selection Clang's BSC-blind standard arg conversion runs.

**Candidates:**
1. **const-drop on a `_Borrow`/raw pointee in a hetero-redecl call arg** — non-strict (:470) strips const & "lets Clang handle later". If Clang's follow-up arg conversion does NOT re-check the const for a `_Borrow` pointee → write through const. The strict path rejects this (:488). Top candidate.
2. **nested BSC qual at pointee** — folds into F76 (Impl :482 canonical-unqual compare drops nested quals). Same Impl root → FOLD.
3. **void* relaxation in non-strict** (:460-465) lets `void*` bind a `_Borrow` arg in a hetero-redecl call — `AreBSCPointerQualifiersCompatible` (:432, shared) runs FIRST and rejects borrow/owned mismatch, so outer owned/borrow can't be laundered to void*; pointee-level only. Likely sound at outer level.

Top: candidate #1 (const-drop in hetero-redecl call arg via non-strict).

## IsSafePointerConversion (SemaBSCSafeZone.cpp:722-753) — UNPROBED

**Invariant**: in `_Safe` zone, a pointer-to-pointer implicit conversion `SrcCanPtr -> DstCanPtr` is allowed only by one of the explicit relaxed cases (drop `_ArrayElem` on borrow, add-const on borrow pointee, owned-to-void-owned, trivial-borrow-to-void-borrow) OR `SrcCanPtr == DstCanPtr` canonically.

**Peers**:
- `IsSafeBuiltinTypeConversion` (line 89) — non-pointer scalar matrix, sibling to this for the non-pointer case in `IsSafeConversion`.
- `IsSafeFunctionPointerTypeCast` (line 486) — fnptr-specific implicit conversion.
- `CheckBorrowQualTypeCStyleCast` (SemaBSCOwnership.cpp:642-644) — F27 (IJOKAC) — explicit C-style cast path. F27 already established the predicate-disagreement between explicit and implicit conversion in the const-borrow dimension. This function is the IMPLICIT side.

**Candidates**:
1. **Reachability — nullability dimension** — the function has zero explicit `_Nullable` / `_Nonnull` handling. The equality check at line 738 relies on canonical `QualType ==` for pointee comparison. If `_Nullable` is normalized away in canonical types, an implicit conversion `int *_Borrow _Nullable -> int *_Borrow _Nonnull` (dangerous: drops nullability info) would silently succeed in `_Safe` zone. Sibling concern to F29 / F56.
2. **Symmetry — `_ArrayElem` not checked outside the borrow-borrow arm** — owned-to-void-owned (742-744) and trivial-borrow-to-void-borrow (746-749) don't check `_ArrayElem`. So `T*_Owned _ArrayElem -> void*_Owned` silently drops `_ArrayElem`. Likely intentional.
3. **Composition** — function takes canonical types. If a caller passes a non-canonical typedef-wrapped BSC-qualified pointer, `isBorrowQualified()` may return false on what is semantically a qualified pointer.

Top: candidate #1 (nullability dimension). Probe: implicit conversion `_Nullable -> _Nonnull` in safe zone.

## IsSafeFunctionPointerTypeCast (SemaBSCSafeZone.cpp:486-579) — RE-PROBED 2026-05-29: SHAPE-REJECTED (prior CONFIRMED-NEW was WRONG)

**CORRECTION (2026-05-29)**: the 2026-05-21 "CONFIRMED-NEW / silent accept" claim is INCORRECT. Re-verified: the nested fnptr safe-zone mismatch (`outer_takes_unsafe_t fp = safe_outer`) is (a) a hard ERROR inside a `_Safe` function ("incompatible function pointer types", 1 error), and (b) a `-Wincompatible-function-pointer-types` WARNING in non-safe context — NOT a silent accept. The runtime SIGSEGV (unsafe cb run via `safe_outer`) only occurs when non-safe code ignores the warning. So the safety contract IS enforced (error in _Safe, warning outside); this is NOT a soundness FN and NOT filable. Distinct from F41 (owned), which was SILENT in non-safe code (owned fully stripped → canonically identical types → no warning) = genuine FN; here the sugar-level _Safe difference triggers the C incompatibility warning. Do not file. (orig analysis retained below)

## IsSafeFunctionPointerTypeCast (SemaBSCSafeZone.cpp:486-579) — [orig 2026-05-21] CONFIRMED-NEW

**Invariant**: A fnptr assignment / cast that converts between two outer fnptr types
must check the safe-zone qualifier of every nested fnptr type (params + return),
not just the outermost function-type's safe-zone.

**Peers**:
- `DoesFunctionPointerSatisfyConstraints` (line 430) — loops params with strict
  pointer comparison but never recursively re-checks safe-zone variance.
- F41 (`CheckOwnedFunctionPointerType`) — same only-outer-level pattern for
  `_Owned`. F53/F56/F57 — redecl peers for borrow/null/heterogeneous owned.
  This site is the *implicit/explicit cast* path, NOT redecl, and the dimension
  is `_Safe/_Unsafe`, not previously covered.

**Candidates**:
1. **Nested fnptr param safe-zone variance** — `outer_takes_unsafe_t fp =
   safe_outer;` where the only difference is the inner fnptr param's safe
   qualifier. CONFIRMED — runtime invokes unsafe code from `_Safe` body.
2. Nested fnptr RETURN-type safe-zone variance. Likely same root.
3. Recursion into struct fields containing fnptr — likely independent
   surface, depends on struct-equiv check.

**FILING NOTE**: defect class C1 sibling-check (only-outer-level for safe-zone
dimension). Distinct fix surface from F29/F41/F53/F56/F57.

## IsSafeBuiltinTypeConversion (SemaBSCSafeZone.cpp:89-150) — PROBED-confirmed-F71 + FULL-MATRIX-SWEEP-SATURATED (2026-05-30)
**Invariant**: the safe-zone scalar conversion matrix EnableToConvert[Dest][Source]
allows only value-preserving implicit conversions (no narrowing, no same-width sign flip).
**F71 (MEDIUM, filed)**: cell [LongLong=11][ULong=5]=Y wrongly allows `unsigned long→long long`
(64-bit sign flip on LP64), while siblings [Long=10][ULong=5]=N and [LongLong][ULongLong=6]=N
correctly reject. Silent value corruption (2^64-1→-1). Distinct from F51 (IsSafeConversion enum
block); F51's writeup wrongly cited this matrix as correctly handling same-width sign flips.
repro/F71_safezone_ulong_to_longlong_signflip.cbs.

**FULL-MATRIX SWEEP (2026-05-30, Chain S, bsc-explorer) — NO DISTINCT WRONG CELL.**
Decoded all 225 cells + the two escape hatches and modeled the TRUE accept/reject verdict:
1. Static matrix `EnableToConvert` (:104-119) checked FIRST.
2. Dynamic fallback (:141-147): same-signedness, src-width ≤ dst-width, both integral,
   non-bool, src≠dst → accept (covers ILP32 ULong==UInt etc.).
3. Value-range escape (IsSafeConversion :927-939): `IsSafeConstantValueConversion ||
   DoesExprValueRangeFitInType` can FLIP a matrix-reject to accept when GetExprRange
   provably fits the target.
After all three layers, the ONLY unsound ACCEPT in the implicit path is F71
([LongLong][ULong]). Every other same-width sign flip is correctly rejected:
UInt→Int rejected (value-range 32<32 false), ULong→Long rejected (64<64 false),
all signed→unsigned rejected. **The "converse FP" [Long][UInt]=N cited in F71's
writeup is NOT a real false positive on the current compiler** — `unsigned int u;
long x = u;` ACCEPTS via DoesExprValueRangeFitInType (32 < 64). Retract that note.
- Float dim SOUND: double→float / long-double→double narrowing rejected, float→double
  widen accepted; int→float lossy-but-allowed is by-design (BSC rule allows int→float).
- Value-range path SOUND: `signed%256→uchar` rejected (negative half kept),
  `x&0xFF→uchar` accepted; same-width unsigned (ULongLong→ULong) accepted via fallback.
- Explicit float→enum rejected (enum block :983 else default-rejects float source;
  the :903 `isIntegerType()` guard is false for enum but the enum block closes the hole).
**VERDICT: Chain S SATURATED @ 28656aa9 for unsound accepts.** The matrix-cell defect
class (hardcoded LP64 cell ignoring same-width signedness) has exactly one live builtin
exemplar (F71) + one enum-path exemplar (F51: `size>=` at :1037-1039 ignoring signedness
— same root, would FOLD). Reopen if the matrix, the :141-147 fallback, or
DoesExprValueRangeFitInType is edited. Full per-probe ledger in `_probed.md` 2026-05-30.

## IsSafeConstantValueConversion (SemaBSCSafeZone.cpp:152-226) — 2026-05-29
**Invariant**: a compile-time constant is exempt from safe-zone narrowing iff its
value fits the destination exactly. Int: bit-width check with sign handling
(negative→getMinSignedBits, positive-signed needs width>activeBits, positive-unsigned
width>=activeBits, negative→unsigned dest=false). Float: float→float allowed iff
!Truncated; int→float allowed iff round-trips exactly.
**Peers**: IsSafeBuiltinTypeConversion (F71), IsSafeConversion (F51).
**Candidates**: 1. off-by-one at signed boundary (127/128, -128). 2. int→float
round-trip edge (2^24, 2^24+1). 3. unsigned dest boundary (255/256).
**Probe outcome (2026-05-29): PROBED-SOUND.** All 8 boundaries exact: schar 127✓accept/128✓reject/-128✓accept/-129✓reject; uchar 255✓/256✓; int→float 2^24✓accept/2^24+1✓reject. Fit-check (bit-width+sign, exact float round-trip) correct. The CONSTANT path is sound; F51/F71 are in the NON-constant paths (enum block / builtin matrix).

## EnumDestContainsAllValuesOfSource (SemaBSCSafeZone.cpp:585-616) — PROBED-INCONCLUSIVE 2026-05-29
**Invariant**: explicit enum→enum cast allowed in safe zone iff dest enum contains
every source enumerator VALUE (compared via APSInt `==`).
**Latent hazard (NOT a confirmed defect)**: `Val == DestVal` compares enumerator
APSInts from two different enums. `APSInt::operator==` asserts `IsUnsigned` match
and `APInt::operator==` asserts equal `BitWidth` (APSInt.h:164 / APInt.h). Two enums
with different-width/sign enumerators would assert-CRASH in an assert-enabled build.
**Probed (release build, asserts off)**: NO observable defect. en1 (Big 64-bit vs
Small 32-bit) rejected cleanly; en3 (dest contains -1 at 64-bit) allowed; en5 (dest
lacks -1) rejected; en2 (-1 → large-unsigned enum) allowed but CORRECT (−1 bits =
valid dest enumerator, no out-of-range UB). The U.VAL comparison happens to give
semantically-right answers for constructed cases. Not filed: no FP/FN in release;
the assert-crash is debug-build-only and I could not trigger genuinely mismatched
widths (Clang appears to normalize enumerator APSInt widths for these cases).

## CheckBSCSafeZoneBitfieldAssign / DoesExprValueRangeFitInBitWidth (SemaBSCSafeZone:782, SemaChecking:12672) — PROBED-SOUND 2026-05-29
**Invariant**: in safe zone, an integer assignment to a bit-field must fit its width
(value range). BSC wrapper gates out _Bool bitfields (handled by IsSafeConversion) and
non-integral RHS; delegates to upstream GetExprRange/IntRange via DoesExprValueRangeFitInBitWidth
(with the correct strict-`<` edge for signed-target + non-negative source).
**Probed SOUND**: unsigned:3 7✓/8✗; signed:3 3✓/4✗ (max), -4✓/-5✗ (min). All boundaries
correct. Reuses well-tested upstream machinery; no BSC-specific gap. No bug.

## _Safe/_Unsafe-block boundary vs ownership/borrow ledger (cross-analyzer, 2026-05-29 bsc-explorer chain trace) — PROBED-SOUND (no new root cause)

**RESULT (8 probes, all SOUND):** the `_Unsafe{}` block boundary does NOT drop any
ownership/borrow obligation. Ownership+borrow analyzers are safe-zone-AGNOSTIC (whole-CFG,
no gating), so use-after-move, leak-at-scope-exit, double-free, borrow-exclusivity, owned-init,
and field-granular move all fire identically inside vs outside `_Unsafe`. Only init-analysis
(BSCIRBuilder SafeZoneStack) is `_Unsafe`-suppressed, and ownership's own CFG-based uninit-owned
check covers the owned case independently. Raw/owned cast is rejected even in `_Unsafe` (P8);
`__move_to_raw`/`__take_from_raw` transfers are correctly ledger-tracked (P9, valgrind-clean).
See `_probed.md` 2026-05-29 for the full P1-P9 table. Structural notes + candidates retained below
for provenance; all closed.

**Chain-trace structural findings:**
- `BSCOwnership.cpp` + `BSCBorrowChecker.cpp` have ZERO safe-zone gating (grep `SafeZone|Unsafe|SZ_` = no logic hits). The ownership + borrow analyzers run over the WHOLE CFG of the function regardless of `_Safe`/`_Unsafe` block nesting. So `_Unsafe { consume(p); }` IS tracked by ownership; an outer `_Safe` use-after-move SHOULD be caught.
- Only `BSCIRBuilder` (init analysis IR) has `SafeZoneStack`/`currentSafeZone()` (:44-47, push/pop at :263/:351). Init suppression of `_Unsafe` is real (prior explorer confirmed) and lives here.
- Analyzer GATING (SemaDeclBSC.cpp:279-401):
  - `RequireBorrowCheck = FindSafeFeatures(FD)` → `SafeFeatureFinder::FindOwnedOrBorrow` (WalkerBSC.h:367-461). NO `VisitSafeStmt` override → `_Unsafe` block contents ARE traversed via `VisitStmt` children. So owned/borrow inside `_Unsafe` still turns ownership ON. (confirmed by reading)
  - `RequireNullabilityCheck` (NC_SAFE default) needs `HasSafeZoneInFunction(FD)`.
  - **Ownership runs ONLY when `NumNullabilityCheckErrorsInCurrFD == 0`** (line 395). So a nullability error short-circuits ownership. Possible interaction: a function with a nullability *false positive* suppresses ownership leak detection? (low priority — FP would itself be a bug)
  - **Ordering:** init-analysis (BSCIR, `_Unsafe`-suppressed) and ownership-analysis (CFG-based, NOT suppressed) are SEPARATE passes. A use-of-uninit obligation that init drops inside `_Unsafe` is NOT recovered by ownership (ownership tracks owned-move/leak, not scalar-init).

**Candidates (boundary-crossing shapes to differentiate):**
1. Reachability — **owned value moved/consumed inside `_Unsafe`, used after in `_Safe`**: ownership should catch use-after-move (no suppression). Probe to verify NOT a hole (expect REJECT). If ACCEPTED → ledger hole.
2. Reachability — **owned leaked: `_Owned p = mk(); _Unsafe { /* nothing */ }` then scope-exit** — does the `_Unsafe` block presence change the leak-at-scope-exit verdict? Expect leak REJECT either way.
3. Composition — **double-free across boundary: `_Unsafe { safe_free(p); }` then outer scope-exit auto-drop / second free** — does ownership see the free inside `_Unsafe` and clear the owned obligation, or does it double-count? KEY: BSC has no auto-drop, so a manual `safe_free` then leak-check; does moving the free into `_Unsafe` confuse the move/consume ledger?
4. Composition — **init obligation the `_Unsafe` block performs but ownership relies on**: a field/var initialized ONLY inside `_Unsafe`, then read in `_Safe`. Init is suppressed inside `_Unsafe` (won't complain about the read's source), but does the read see it as init?

## E5 wellformedness-sweep (2026-05-30 bsc-explorer) — _Owned/_Borrow/_ArrayElem-on-non-pointer + IsUnsafeType site coverage

**Target predicates + where each gate actually lives:**
- `CheckOwnedQualifierOnNonPointerType` (SemaBSCOwnership.cpp:189-243) — decl-spec gate, deep BASE-strip
  (strips all ptr+1 array level to the innermost base, then checks `isOwnedQualified && !isValidOwnedType`).
  Called at 5 decl sites: SemaDecl.cpp:7096 (var), 7858 (var2), 10092 (ret), 15337 (param), 18691 (field).
- **NO `CheckBorrowQualifierOnNonPointerType` exists.** The borrow-on-non-pointer + arrayelem-on-non-pointer
  + owned/borrow-on-fnptr checks all live INLINE in `BuildQualifiedType` (SemaType.cpp:2042-2077), which runs
  during EVERY type construction (decl, typedef, cast, param, return) — so those are uniformly covered (verified:
  typedef/cast/decl all REJECT `int _Borrow`, `int _ArrayElem`, raw `*_ArrayElem`).
- `CheckArrayElemQualifierRules` (SemaBSCOwnership.cpp:69-106) — WORKLIST recursion (ptr pointee + array elem)
  enforcing (a) arrayelem-on-non-pointer, (b) arrayelem-requires-owned/borrow, (c) `HasInvalidArrayElemPointee`.
  Called via `CheckArrayElemQualifierOnType` at the SAME 5 decl sites. The deeper rules (b)(c) are decl-site-only,
  BUT (a)(b) at top level are ALSO in BuildQualifiedType so cast/typedef catch the top level.
- `IsUnsafeType` (SemaBSCSafeZone.cpp:1081-1126) — recurses ptr/struct/array; returns true ONLY for `BuiltinFn`
  and `va_list`. Called at SemaType.cpp:5549/5556 (safe-fn ret/param type) + template-instantiation sites.
  Narrow (va_list/builtinfn); does NOT recurse into FunctionProtoType params (a fnptr param taking va_list is
  not flagged) — but va_list-in-safe-zone is a thin soundness surface, deprioritized.

**Probed (E5):**
- typedef `int _Owned`/`int _Borrow`/`int _ArrayElem` → ALL REJECT (BuildQualifiedType). Covered.
- cast `(int *_ArrayElem)q` raw arrayelem → REJECT (BuildQualifiedType arrayelem-requires-owned/borrow). Covered.
- decl `int *_ArrayElem p` raw arrayelem → REJECT. Covered.
**E5 VERDICT (no-new-pattern):** the family is UNIFORMLY covered. The reason most gaps
close: BuildQualifiedType (SemaType.cpp:2042-2077) runs the borrow/arrayelem-on-non-pointer
+ owned/borrow-on-fnptr checks INCREMENTALLY per pointer-construction level at EVERY site
(decl/typedef/cast/param/return/field), so the decl-spec-only deep predicates
(CheckOwnedQualifierOnNonPointerType deep-strip, CheckArrayElemQualifierRules worklist) are
largely redundant — a qualifier on a base/nested non-pointer is caught the moment that level
is built. Add/drop of `_ArrayElem` across assignment/cast goes through IsSafePointerConversion
(:811-824, Chain I SOUND): AE-add rejected (no single→array promotion), AE-drop allowed by
design. Site-uniformity verified across global/local/param/return/field/union/typedef/cast for
owned/borrow/arrayelem-on-non-pointer, arrayelem-requires-owned/borrow, and HasInvalidArrayElemPointee
(nested-borrow pointee) — all 6+ sites REJECT identically. F81 was the LAST real site-parity gap
here. Full coverage table in /tmp/probed_E5.md.

**One ACCEPT cell (backstopped, NOT fileable):** `va_list` as a PARAM of a `_Safe` function is
accepted (exit 0) because IsUnsafeType (:1081) runs on the DECAYED param type
(`struct __va_list_tag *`) and recurses ptr→struct→fields, none of which are va_list/BuiltinFn,
so the source-spelled va_list classification is lost. The va_list local form is also accepted at
decl. BUT every va_start/va_arg USE is itself an `_Unsafe` builtin call (rejected in the safe
zone), so a `_Safe` fn can never consume the va_list → decl-time inconsistency only, NOT a
soundness FN, and OUTSIDE the _Owned/_Borrow scope. Same backstopped-LOW shape as F82.

## CheckBorrowQualTypeCStyleCast (SemaBSCOwnership.cpp:605-666) vs CheckBSCQualTypeAssignment (:482) — CONFIRMED-new 2026-05-30 (bsc-explorer chain trace)

**Invariant**: an explicit C-style cast must be at-least-as-strict as the
assignment-conversion when it comes to DROPPING a `_Borrow` qualifier — dropping
a borrow (outer OR nested) must be rejected in both paths (it defeats lifetime
tracking). Mirror of F27 (which was the false-POSITIVE direction; this is the
false-NEGATIVE direction).

**Root cause (TWO co-located gaps, one fix surface = the borrow cast path):**
1. **Dispatch gap** — SemaCast.cpp:2927-2936 fires `CheckBorrowQualTypeCStyleCast`
   only when the OUTER type `isBorrowQualified()`. There is NO `hasBorrowInPtrChain`
   peer to the owned path's `hasOwnedInPtrChain` (SemaCast.cpp:2937-2960). So a
   cast `int *_Borrow *` -> `int **` (outer raw on both sides, `_Borrow` nested)
   never even calls the borrow check → nested borrow silently dropped.
2. **Predicate gap** — even single-level `int *_Borrow` -> `int *` reaches
   `CheckBorrowQualTypeCStyleCast` (outer is borrow) but returns true via
   `IsUnqualifiedTypeMatch` (SemaBSCOwnership.cpp:655), because
   `hasSameUnqualifiedType(int*, int*_Borrow)` is true. The borrow→raw DROP
   direction is not rejected (only the const-borrow MISMATCH at :642-644 and
   cast-away-const at :663 are).

**Asymmetry baseline**: the ASSIGNMENT form of either conversion is REJECTED —
`CheckBSCQualTypeAssignment` (SemaBSCOwnership.cpp:486-491) sets
`MayHaveBorrow |= LHSPtr->hasBorrowFields()` (TypeBSC.cpp:81-90, recurses the
pointer chain) → runs `CheckBorrowQualTypeAssignment` which rejects the drop.
So `int **raw = pp;` errors "incompatible _Borrow types" while `(int**)pp`
compiles clean.

**Soundness**: runtime use-after-scope. Non-safe BSC fn casts away the borrow,
stores a borrow of a callee local into a caller-visible raw/borrow slot; after
the callee returns the slot dangles and reads a clobbered dead frame
(0xdead0005). repro `/tmp/explorer_uaf.FsDBvN.cbs`, single-level escape-to-global
`/tmp/explorer_singlelevel.0QZqlX.cbs`. In `_Safe` BOTH paths are rejected
(IsSafePointerConversion catches it); the hole is non-safe-context only.

**Blast radius**: owned nested-pointer cast `int *_Owned *` -> `int **` has the
SAME shape — `CheckOwnedQualTypeCStyleCast` short-circuits at SemaBSCOwnership.cpp:310
(`LHSRaw && RHSRaw → return true`) before recursing into the owned pointee, so
nested owned is dropped too (plain data pointer, NOT fnptr → distinct surface
from F41/F74/F76 which are fnptr-variance). A single fix to the borrow/owned cast
predicate (detect drop at any nesting, mirror `hasBorrow/OwnedFields`) closes both.

**DISTINCT from**: F27 (outer const-borrow ADD direction, false-positive — opposite
sign), F41/F74/F76 (function-pointer param/return variance — different type
constructor and different predicates). Defect class **C1** (Ignore-asymmetry:
cast path vs assignment path disagree on the borrow-drop conversion).

## IsSafePointerConversion (SemaBSCSafeZone.cpp:801-833) — PROBED (see candidate 1)

**Invariant**: a safe-zone pointer conversion is allowed only if it cannot violate
type/const/ownership safety — borrow→borrow permits {drop `_ArrayElem`, add const,
identical pointee, trivial-pointee → void}; owned → `void*_Owned` (free idiom);
otherwise only when the full qualified pointer types are identical (fallback :832).
**Peers**: `IsSafeConversion` (:843 caller, picks Src/Dst canonical), 
`IsSafeFunctionPointerTypeCast` (:564), `GetSafeArrayDecayType` (:835).
**Candidates**:
1. **trivial→void borrow cast requires `DstPointeeIsConst == SrcPointeeIsConst`
   (:822) → rejects adding const (`int*_Borrow → const void*_Borrow`) —
   PROBED-confirmed-FP-LOW**. `void*_Borrow` form compiles clean; `const
   void*_Borrow` rejected ("forbidden in the safe zone") though add-const+void is a
   safe widening. LOW (rare construct, workaround = `void*_Borrow`); not filed.
   probe: probes/safezone_borrow_to_const_void_cast.cbs.
2. **owned→`void*_Owned` (:827) has NO triviality/subfield check** (unlike the
   borrow void-cast) — but FOLDS to F91 territory: Sema allows the free idiom by
   design; the BSCOwnership analysis is the subfield-leak safety net (F91 is its gap).
3. **trivial→void allows `T*_Borrow _ArrayElem → void*_Borrow`** (drops `_ArrayElem`
   + goes void) — loses bounds; likely sound for a borrow (void deref restricted). UNPROBED.

## safe-zone raw-deref restriction wrapper-strip (C1 on a zone gate) — probing
**Invariant**: dereferencing a RAW pointer in `_Safe` is forbidden ("'*' operator is
forbidden in the safe zone") regardless of paren/comma/cast wrapper — a bypass would
let _Safe code read/write arbitrary memory (HIGH).
**Peers**: F36 (string-literal &_Mut wrapper), C1 class, the safe-zone gate.
**Candidates**:
1. **`*(0, p)` / `*(p)` raw deref in _Safe bypasses the restriction? — probing**.
2. cast-wrapped `*(int*)p`. UNPROBED.
3. subscript `p[0]` as deref-equivalent (already gated separately, cycle 19). 

## IsSafePointerConversion const-to-void* asymmetry (FP) — probing
**Invariant**: adding const is safe (more restrictive). Same-type add-const is allowed
(815-817 normalize); but the void* path (821) requires `DstConst==SrcConst`, so
`int*_Borrow` → `const void*_Borrow` (add const + erase to void) fails const-match →
strict fallback → DISALLOWED. Over-restriction = precision FP.
**Peers**: F51 (IsSafeConversion), IsSafeFunctionPointerTypeCast (filed), void* trivial case.
**Candidates**:
1. **`int*_Borrow` → `const void*_Borrow` (non-const src) → rejected? — probing** (FP).
2. `const int*_Borrow` → `const void*_Borrow` (const match) → allowed (control).

## IsSafeFunctionPointerTypeCast launders nullability? (F100 sibling) — probing
**Invariant**: the assignment path checks nullability compat (CheckNullabilityQualType...);
if IsSafeFunctionPointerTypeCast (:564) doesn't, a cast `(FPN)nullable_returning` launders
_Nullable→_Nonnull → caller derefs result without check → null-deref FN. Same root as F100.
**Candidates**: 1. cast nullable-returning fn to nonnull-fnptr, deref result → FN?

## IsSafeFunctionPointerTypeCast launders owned/borrow? (F100 sibling) — probing
**Invariant**: the cast path (→ DoesFunctionPointerSatisfyConstraints) must check owned/borrow
fnptr param/return qualifiers (assignment path does, via CheckOwnedFunctionPointerType).
If not, casting a borrow-fn to an owned-fnptr (or vice versa) launders ownership → leak/UAF.
**Candidates**: 1. `(FPO)takes_borrow` (FPO takes owned) → rejected? else ownership launder.

## ensure_init_if_ret check at fnptr RETURN position (F100 sibling) — probing
**Invariant**: returning a no-contract fn as a contract-fnptr return type must be checked
(like assignment rule 5), else laundered like the cast (F100). 
**Candidates**: 1. `FP0 get(void){ return bar; }` (bar no contract) → rejected? else launder.

## range-inbound implicit conversion (0dafa404, DoesExprValueRangeFitInType) — probing
**Invariant**: implicit narrowing (int→char) allowed only if E's static value range fits the
target; an out-of-range value must still be rejected. A wrong "fits" = silent truncation FN.
**Candidates**: 1. value 200 (>char max 127) via tracked var → rejected? 2. range boundary off-by-one.

## bitfield assign check REVERTED (d8d995b) — probing
**Invariant**: assigning a too-wide value to a narrow bitfield in _Safe should be range-checked
(like int→char narrowing). The check was implemented (76c8e59) then REVERTED (d8d995b) → may now
be unchecked → silent truncation.
**RESULT (2026-06-08): CONFIRMED gap but KNOWN/reverted — NOT filed.** `unsigned v; s->x=v` for `unsigned x:3`
→ compiles CLEAN (no width check), silent runtime truncation (v=100 → x=4 via &7). Asymmetry: regular
narrowing `*c=v`(int→char) REJECTED, but bitfield-width narrowing UNCHECKED. This is the check `76c8e59`
implemented and `d8d995b` REVERTED — maintainers already aware (deliberate revert, presumably pending a
non-buggy reimpl). Per "don't file known/intentional gaps" discipline → NOT filed. Precision issue (silent
value truncation, not memory-unsafety). Re-check after a future fork commit reinstates the bitfield check.
NOTE: signed→bitfield (`int v; s->x=v` where x is unsigned) IS caught — by the general int→unsigned
signedness check, not a width check (red herring).

## ++/-- produce VOID in _Safe (2026-06-08) — INTENDED design
`x++`/`x--`/`++x`/`--x` in the safe zone produce VOID — usable only for side effect (as a statement),
NOT for their value. `x++;` (stmt) OK; `int m = x--;` / `while(n--)` / `while(n-->0)` → ERROR
"conversion from type 'void' to 'int' is forbidden ... note: prefix/postfix '++' and '--' in safe zone
produce void; use only for side effect". Deliberate (avoids the C pre/post-increment value footgun; the
pointer-++/-- rework 97cf8c4b is consistent). Idiomatic `while(n--)` must be rewritten `while(n>0){...;n--;}`.
NOT a bug — the diagnostic itself documents the intent. (Caught while resolving the do-while continue candidate.)

## CanBeUninitializedInSafeZone (:1131) — read, SOUND (in-scope)
Returns false (must-init) ONLY for owned-struct types (recursively, Visited-guarded) — that's the
_Owned-struct must-init rule (OOS). For everything in-scope (regular struct w/ owned field, borrow,
array) returns true; the init dataflow then tracks actual uses. No in-scope gap (uninit borrow/owned-field
declarable, uses caught by init analysis per the known pitfall).

## DoPointerTypesSatisfyAssignmentConstraintsImpl (:395) — read, checks owned/borrow not nullability (F29/F66)
Used by regular ptr assignment (AllowImplicitConversions=true) + fnptr assignment (strict=false).
Checks AreBSCPointerQualifiersCompatible (owned/borrow) + pointee (exact for fnptr/strict, Clang-typecheck
for regular). Does NOT check nullability — regular assignment nullability via CheckNullabilityQualTypeAssignment
(F66 area); fnptr assignment nullability NOWHERE = F29 (filed). Re-confirms F29/F66; no new gap.

## IsSafeBuiltinTypeConversion (:89) — read, SOUND (conversion matrix)
EnableToConvert[dest][src] 15x15 matrix over SafeZoneMap order (Void,Bool,U{Char,Short,Int,Long,LongLong},
S{Char,Short,Int,Long,LongLong},Float,Double,LongDouble). Correctly: forbids integer narrowing (Long→Int=N),
signed↔unsigned value-change (Int↔UInt=N, SChar↔UChar=N), float-precision-loss (Double→Float=N); allows
widening + Bool→wider + int→float (standard C, precision-loss-for-large-ints is intended like C, not a range
narrowing). ILP32 same-signedness ≤-width fallback (:141-147). Pairs with IsSafeConversion (F101 pointer→_Bool
is a SEPARATE path; this is builtin↔builtin). No narrowing FN.

## IsUnsafeType (:1081) — read, narrow (va_list/BuiltinFn only)
Flags ONLY __builtin_va_list / BuiltinFn as unsafe (recurses ptr/struct/array, Visited-guarded; owned-struct
skipped=OOS). Raw pointers / fnptrs / unsafe-fn-pointers NOT flagged here — gated by OTHER checks
(raw-ptr deref/arith restrictions, fnptr safe-zone cast). Narrow by design; the broader unsafe-construct
rejection is distributed. No hole (verified raw-ptr deref restricted separately below).

## IsSafeConstantValueConversion dependent-deferral (:170-244) — read, SOUND (generics campaign 2026-06-17)
Line 174 `if (E->isValueDependent() || E->isInstantiationDependent()) return true;` defers the
constant-fit check at TEMPLATE-DEFINITION time (comment: "Defer the safety check until instantiation").
USER HYPOTHESIS (this session): the fix might SILENCE rather than DEFER → soundness FN for generics.
DISPROVEN. The deferral is a genuine DEFER: every safe-zone / dataflow check re-runs at INSTANTIATION
with concrete types, value-sensitively.
- Mechanism (the real safety net): `BSCDataflowAnalysis`/AnalysisBasedWarnings are gated on
  `isDependentContext()` (AnalysisBasedWarnings.cpp:2224 `return;`) + `ActOnFinishFunctionBody`
  (SemaDecl.cpp:16642-62) only analyzes concrete FunctionDecls. Primary/uninstantiated function-template
  bodies are NEVER analyzed (so no crash from getTypeSize on a dependent pointee; X1-X4 confirm). Each
  instantiation is a concrete FunctionDecl → full Sema conversion check + ownership/init/nullability/borrow
  re-run.  NOTE: comment at :16648 says "Skip function template and class template" but code only explicitly
  skips class-template members (getDescribedClassTemplate); free fn templates skipped via dcl/dependent-ctx.
  Benign — empirically no dependent free-fn-template body reaches analysis.
- Asymmetry (benign/dead): :211 and :224 (float branch) guard only `isValueDependent()`, but :174 already
  early-returns for value-OR-instantiation-dependent, so the float branch is only reached for non-dependent E.
- PROBED-shape-rejected as a bug surface. ~68 differential probes (generic vs byte-identical non-generic),
  ALL matched: narrowing return/init (G1/CO/NTV value-sensitive), raw deref (G2), use-after-move/leak (UAM/LK),
  uninit (UI), ensure_init_if_ret (EI), nullability nonnull return/param/call-site (NB1-3), _Safe-block (A4),
  borrow/ptr cast (A2), generic-struct owned-field leak (OF1/OF2), const-generic overflow (MIX3), per-spec
  caching both orders (MIX1/2/4), NLL dangle (substitution-rejected), deduced calls (AC3/AC4), -rewrite-bsc
  monomorphization (RW/RW2/RW3 clean), runtime valgrind (RT: 0 leaks/0 errors). NO FN, NO FP, NO crash.
  Residual (NOT a bug, standard template semantics): uninstantiated templates aren't analyzed, so a
  T-independent violation (e.g. `_Nonnull` fn returning nullptr) is silent until instantiated — any USE fires
  it (NB3i); no codegen for uninstantiated → no soundness/runtime hole. Not fileable.

## IsSafeConversion char-pointee string exemption (SemaBSCSafeZone.cpp:872-884) — UNPROBED 2026-06-18

**Invariant**: the FIRST pointer block in IsSafeConversion is a fast-path EXEMPTION that
runs BEFORE the IsSafePointerConversion matrix. It returns `true` (conversion safe) for
TWO cases without ever consulting the owned/borrow/qualifier matrix:
(a) `isa<CXXNullPtrLiteralExpr>(E->IgnoreParens())` — nullptr init of any pointer (intended);
(b) `DestType->getPointeeType()->isCharType()` AND `isSafeZoneStringType(E)` — string-literal
/ char-array source into any char-pointee destination.

**The hole**: case (b) gates ONLY on (1) dest pointee `isCharType()` and (2) E being a
string literal OR a char-element array (`isSafeZoneStringType`, SemaExpr.cpp:6581 — does NOT
require const, does NOT inspect the dest's OWNED/BORROW qualifier). It then `return true`,
bypassing IsSafePointerConversion entirely. So a `char *_Owned p = "abc";` (string literal
into an OWNED pointer) is approved by IsSafeConversion: the literal lives in `.rodata`
(static storage, never heap-allocated), but the destination is `_Owned` → the ownership
ledger now believes `p` owns a heap block. At scope exit / explicit free the runtime calls
`free()` on a `.rodata` address → invalid free (UB). The byte-identical `int *_Owned`
form has NO such exemption (pointee not char) and is correctly rejected.

**Peers**: IsSafePointerConversion (:818, the matrix this bypasses — its owned arm only
permits `void*owned ← T*owned`, NOT raw→owned); CheckOwnedQualTypeAssignment / the ownership
analyzer (BSCOwnership — the leak/free safety net, may or may not see the string-literal
as a non-heap source); F36 (mut-borrow of string literal, a DIFFERENT gate, SemaExpr).

**Candidates (ranked)**:
1. [reachability] `char *_Owned p = "abc";` — **PROBED-SHAPE-REJECTED 2026-06-18.** Rejected by a
   SEPARATE owned gate: `error: incompatible _Owned types, cannot cast 'char[4]' to 'char *_Owned'`.
   So IsSafeConversion case-(b) is BACKSTOPPED by CheckOwnedQualTypeAssignment for the owned case;
   the exemption only fires for raw/borrow char dests. Not a soundness hole.
2. [composition] `unsigned char *p = "abc";` — **PROBED-SHAPE-REJECTED.** Just the standard C
   `-Wpointer-sign` warning (exit 0); `unsigned char*` is a raw pointer, deref forbidden in _Safe
   anyway. No soundness issue.
3. [boundary] `char *_Borrow p = local_char_array;` — **PROBED-INCONCLUSIVE** (the uninit-array
   init error fired first; would need an initialized array; borrow-no-static-origin likely catches
   the literal form). De-prioritized: the borrow case is also caught (string→mutable-borrow already
   rejected "immutable", _probed.md 4994).
VERDICT: the char-pointee string exemption is uniformly backstopped (owned gate, borrow-immutable
rule, raw-deref forbidden). No new root cause here.

## IsSafePointerConversion (:818) — candidates 2026-06-17 (was UNPROBED per INDEX)
INVARIANT: safe-zone ptr↔ptr cast allowed only for: add-_ArrayElem-removed / add-const / erase-trivial-to-void(borrow) / T*owned→void*owned; else SrcCanPtr==DstCanPtr.
Candidates:
1. [composition] multi-level borrow `T*_Borrow*_Borrow` → `const T*_Borrow*_Borrow`: removeLocalConst is OUTER-only → classic `const T**` hole; can it launder a write-through-const in safe zone? **UNPROBED** (top)
2. [reachability] erase `T*_Borrow`→`void*_Borrow` (T trivial) then round-trip to `U*_Borrow` (different) — type confusion? (un-erase likely rejected). UNPROBED
3. [boundary] struct-with-owned-field `*_Borrow`→`void*_Borrow`: is owned-field struct "trivial"? if yes, owned laundering. UNPROBED

## IsSafeConstantValueConversion (SemaBSCSafeZone.cpp:170-244) — read 2026-06-24
**Invariant**: a CONSTANT conversion is safe in the safe zone iff the value fits the dest exactly:
int→int positive→signed needs `width > activeBits` (sign bit), →unsigned `width >= activeBits`, negative→
signed `width >= minSignedBits`, negative→unsigned always false; float→float iff no precision loss;
int→float iff round-trip-exact; else false.
**Peers**: IsSafeConversion (caller), IsSafeBuiltinTypeConversion.
**Candidates**: 1. **boundary off-by-one: 128→signed char / 256→unsigned char accepted (FN) vs rejected** UNPROBED ⭐.
2. int→float round-trip edge. 3. negative→unsigned.

## IsSafeBuiltinTypeConversion matrix (SemaBSCSafeZone.cpp:98-168) — read 2026-06-24
**Invariant**: EnableToConvert[Dest][Src] encodes value-preserving widenings allowed in the safe zone;
unsigned-src→signed-dest equal/larger width guarded to false (:148, F71). Question: int→float entries
([12][10] Long→Float, [12][11] LongLong→Float = Y) are PRECISION-lossy — does the matrix allow them
IMPLICITLY, contradicting the manual "implicit int→float forbidden in safe zone"?
**Peers**: IsSafeConversion (caller, splits implicit/explicit), IsSafeConstantValueConversion (constant leg).
**Candidates**: 1. **implicit `float f = long_var;` allowed (FN/conformance) vs rejected** UNPROBED ⭐.
2. uint→float precision. 3. the matrix unsigned/signed asymmetry beyond the :148 guard.

## IsSafeConversion dispatch (SemaBSCSafeZone.cpp:867+) — read 2026-06-25 (safe-zone conversion gate)
INVARIANT: gate safe-zone conversions. ZONE-GATED (`!IsInSafeZone() → return true`, _Unsafe allows all); fnptr →
IsSafeFunctionPointerTypeCast (F100 area); nullptr→any-ptr allowed; char* from string-literal allowed; recurses
into ConditionalOperator arms (IgnoreParenImpCasts); then IsSafePointerConversion (:825) + IsSafeBuiltinTypeConversion
(:98) + IsSafeConstantValueConversion (:170) for the actual checks.
CANDIDATES (sound, no new): (1) comma launders a lossy const conversion — REFUTED (both `char c=300` and
`char c=(0,300)` rejected; the result-type/value check catches the comma); (2) ternary handled by recursion. The
findings F15/G15 (borrow add-const+void-erase arm), F102 (owned add-const arm), F100 (fnptr-cast, now fixed) live
in IsSafePointerConversion's specific arms, NOT the dispatch. IsSafeConversion dispatch SOUND. Safe-zone conversion
machinery documented.

## SelectDeclForHeterogeneousRedecl (SemaBSCSafeZone.cpp:254) — heterogeneous safe/unsafe redecl resolution (2026-06-27)
INVARIANT: among a function's safe+unsafe redecls, in a SAFE context selects only a constraint-satisfying SAFE decl (else
nullptr); in an UNSAFE context prefers safe-then-unsafe. PEERS: IsUnsafeType (:1110), the redecl-merge path, codegen def-pick.
CANDIDATES: 1. (decl-vs-definition disconnect = F117) this function picks the safe DECL for a safe caller (correct for
type-check), but the single _Unsafe DEFINITION's body runs → safe caller runs unsafe body. PROBED-confirmed-F117 (open,
heterogeneous-redecl area = user DO-NOT-FILE). 2. (unsafe-context safe-pref order) prefers safe decl in unsafe ctx — sound
(stricter). 3. (CheckConstraints leniency) caller-dependent. The fileable surface here is exhausted (F117/F104 = user area).

## IsUnsafeType (SemaBSCSafeZone.cpp:1110) — unsafe-type-in-safe-zone check (2026-06-27)
INVARIANT: a type is unsafe in a safe zone if it is/contains va_list (__builtin_va_list) or a BuiltinFn type; walks the type
recursively (pointers→pointee, structs→fields) with a Visited set. Used to reject unsafe types in _Safe. PEERS: the safe-zone
body checks, SelectDeclForHeterogeneousRedecl. CANDIDATES: 1. va_list/BuiltinFn flagged (sound). 2. buried unsafe type
(struct field va_list) via recursion (sound). 3. ownership tracking THROUGH an _Unsafe block — PROBED-sound (see below).

## isBorrowArrayDecayTypeMatch (SemaExpr.cpp:546-577) — REOPENED @34883aa1, re-read 2026-06-29

**Invariant**: a C array `T[N]` may decay to a `_Borrow`(/`_ArrayElem`)-qualified
DEST pointer iff (a) dest IS borrow-qualified pointer (raw-ptr dest rejected, :551);
(b) element type equals dest pointee modulo const/volatile/restrict that the dest may
ADD (never drop: the strip at :560-565 only runs when DEST is more-qualified than the
elem); OR (c) dest pointee is `void` and (in safe zone) the element is trivial AND the
const direction is add-only (`!ElemConst || DestConst`, :576). When matched,
`GetSafeArrayDecayType` returns DestPtrType verbatim → downstream
`IsSafePointerConversion(dest,dest)` is a TAUTOLOGY. So ALL soundness of the matched
decay rests on THIS function.

**What changed @34883aa1 vs the old (28656aa9) trace**: the const/volatile/restrict
handling at :560-565 is NEW (old note said "strip only ADDs const, never drops"). The
strip is gated `DestPointee.isConstQualified() && !ElemType.isConstQualified()` — i.e.
only relaxes when DEST has the qualifier and ELEM doesn't (add-const direction). The
drop direction (const elem → non-const dest) is NOT stripped → :567 hasSameType fails →
falls to void branch → :576 returns false for non-void. Looks add-only/sound by reading.

**Peers**: `IsSafePointerConversion` (:825, the ptr-ptr matrix — same const/arrayelem
rules but on canonical pointers); `GetSafeArrayDecayType` (:859, the tautology wrapper);
`MaybeDecayArrayToBorrowArrayElemPointer` (SemaExpr.cpp:579, the rewrite that actually
emits `&_Mut a[0]`); callers at SemaExpr.cpp:6756 (call arg) + :10894 (assignment RHS).

**Candidates (ranked)**:
1. (symmetry) **const-elem array → non-const `T*_Borrow`** decay: type-match rejects it
   (:567 fails, :576 false). But does the FALLBACK path (getArrayDecayedType +
   IsSafePointerConversion) ALSO reject, or does the array-decay branch of IsSafeConversion
   accept const-drop? IsSafePointerConversion has IsDroppingConst guard (:838) BUT that's
   borrow←borrow; getArrayDecayedType gives a RAW `const T*` src → SrcCanPtr != DstCanPtr
   fallback → reject. PROBE to confirm no FN. TOP.
2. (reachability) **volatile/restrict drop**: same :560-565 logic for volatile/restrict.
   `volatile T arr[N]` → `T*_Borrow` (non-volatile) — drops volatile? :562 only strips when
   DEST volatile & elem not; drop direction (elem volatile, dest not) → :567 fails → :576
   non-void false. Looks rejected. Lower value (volatile semantics weaker than const soundness).
3. (composition) **decay creating arrayelem borrow that survives the array** (lifetime):
   array-decay produces `&_Mut a[0]` arrayelem borrow; if the array is a local that the
   borrow outlives → dangling. But borrow-checker liveness should catch (region inference).
   Lower priority — separate analyzer, not the decay-match function.

## DoesFunctionPointerSatisfyConstraints + DoPointerTypesSatisfyAssignmentConstraintsImpl (SemaBSCSafeZone.cpp:526 / :413) — UNPROBED (chain L reopen @34883aa1)
**Invariant**: a fnptr assignment/cast must reject any param/return whose BSC qualifier (owned/borrow/arrayelem/nullability/const) differs incompatibly, AT EVERY NESTING LEVEL.
**Peers**: SelectFunctionDeclForPointerAssignment (:556), IsSafeFunctionPointerTypeCast (:582), CheckBSCFunctionPointerType (SemaExpr.cpp:10330), the SemaExpr.cpp:14870 assign-rewrite. F76 root = :500-502 canonical-unqualified pointee compare; AreBSCPointerQualifiersCompatible (:387) checks owned/borrow/arrayelem at OUTER level ONLY, NOT nullability.
**Structure**: DoesFunctionPointerSatisfyConstraints recurses ONE level (per-param via Impl). Impl checks AreBSCPointerQualifiersCompatible at the OUTER pointer, then compares pointees by getCanonicalType().getUnqualifiedType() (strips owned/borrow/null/const on the pointee). So:
- `int*_Owned*` vs `int**` fnptr param → outer raw==raw, pointee compare strips _Owned → ACCEPT (= F76/F77 nested family).
- nullability is NEVER in AreBSCPointerQualifiersCompatible → relies on a separate path.
**Candidates** (ALL PROBED 2026-06-29, chain-L re-walk @34883aa1 — NO new root cause):
1. **Nested-plain-pointer owned on fnptr param/return** — PROBED-folded-into-F41. `owned_cb_t fp=plain_cb` and owned-nested RETURN both ACCEPTED rc=0 in _Safe. Owned check OUTER-ONLY; borrow twin REJECTED (recurses). NEW DATUM: F41 mitigation note ("safe-zone rejects, only fires non-safe") REFUTED — _Safe ASSIGN also accepts. Same fix surface. Re-verify safe-zone coverage when F41 fixed.
2. **Nullability fnptr-param via CAST path** — PROBED-folded-into-F29. Cast and assign both accept the variance; same root (no nullability check in DoesFunctionPointerSatisfyConstraints).
3. **const-pointee variance on fnptr param** — PROBED-shape-rejected (OUT-OF-SCOPE): plain-C differential shows BSC-clang AND gcc both accept w/ -Wincompatible-pointer-types = standard C fnptr behavior, not BSC surface.
4. **Safe/unsafe fnptr direct assign** — PROBED-SOUND. :650-663 correctly rejects None→Safe and Unsafe→Safe. F117-analog-via-fnptr CLOSED (body-launder needs redecl path).

### Chain I re-walk verdict (2026-06-29 @34883aa1): SOUND — all 3 candidates above PROBED
- Cand 1 (const-elem decay to non-const borrow): PROBED-SOUND (rejected, no FN). Deep/inner const-drop also rejected.
- Cand 2 (volatile/restrict): PROBED — volatile-DROP rejected (sound). But volatile-ADD via decay is OVER-REJECTED ("incompatible _Borrow types") while const-ADD is accepted → asymmetry traced to IsSafePointerConversion :838-848 (const arm only, no volatile/restrict arm) → **FOLDED-into-F102/G15** add-qualifier-widening-FP family (same fn, same C1 class, same fix surface). FP not FN.
- Cand 3 (lifetime escape of decayed borrow): PROBED-SOUND (borrow-return rule rejects; plain-borrow indexing rejected; _ArrayElem gates indexing).
- Net: isBorrowArrayDecayTypeMatch's :560-565 const/vol/restrict strip (NEW since 28656aa9) is add-only/sound; the matched-decay tautology rests on a sound match function. No new root cause. The lone FP folds into the known IsSafePointerConversion const-only-relaxation family.

## Sema::IsSafeConversion (SemaBSCSafeZone.cpp:867) — safe-zone conversion gate (core)
- **Invariant**: in _Safe, forbid pointer↔non-pointer, float→int, and implicit narrowing (high→low precision / wide→narrow); allow nullptr-init, char*←string, explicit arithmetic casts, boolean-eval narrowing; ptr→ptr via IsSafePointerConversion; fnptr via IsSafeFunctionPointerTypeCast.
- **Peers**: IsSafePointerConversion (chain AE/F66), IsSafeFunctionPointerTypeCast (F100/F41), IsSafeBuiltinTypeConversion (F51/F71), F102 (const-add owned).
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: `(long)p` pointer→int → rc=1 "conversion from 'int *_Borrow' to 'long' is forbidden in the safe zone"; implicit `long`→`int` → rc=1 "implicit conversion ... forbidden; use explicit cast"; explicit `(int)l` → rc=0 (arithmetic explicit cast allowed). Core conversion gate sound. (2) array→pointer decay path (GetSafeArrayDecayType + IsSafePointerConversion) — qualifier preservation on decay. (3) IsBooleanEvaluation narrowing exception — a narrowing in a boolean context allowed (hole?).
