# SemaExpr.cpp — BSC borrow/move/owned EXPRESSION-time gates

Source: `clang/lib/Sema/SemaExpr.cpp`. Covers the `&_Mut`/`&_Const` build path
(`GetBorrowAddressOperandQualType`, the `CreateBuiltinUnaryOp` BSC tail at
:17116-17146), and the assignment-constraint gate (`CheckAssignmentConstraints`
:10329 — F74 root). Sibling files: SemaExprMember.cpp (member-access borrow/owned),
SemaBSCSafeZone.cpp (pointee compare — F76 root), SemaBSCOwnership.cpp (CheckMoveVar).

## GetBorrowAddressOperandQualType (SemaExpr.cpp:15325-15405) — read fuller 2026-06-08, well-checked

**&_Mut checks (all present)**: const pointee/input → err_mut_expr_unmodifiable; hasBorrow → err_borrow_on_borrow (non-deref case); FunctionProtoType → err_mut_or_const_expr_func; global-in-safe-zone (getPrimaryDecl→VarDecl global) → err_safe_mut. `&_Mut *p` deref case checks const-pointee. Global array/field resolve via getPrimaryDecl (PROBED-SOUND). No new gap.


**Invariant**: building `&_Mut e` / `&_Const e` must reject borrowing a non-borrowable
operand (a borrow-of-borrow, a const for `&_Mut`, a function, a global in safe zone)
and must compute the result borrow qualifier.
**Peers**: the CreateBuiltinUnaryOp BSC tail (:17116-17146, string-literal gate),
getPrimaryDecl (:15251, decl-strip for the safe-zone-global gate).
**Gates seen**:
- `&_Mut` of borrow → err_borrow_on_borrow (immediate `hasBorrow()`).
- `&_Mut` of const → err_mut_expr_unmodifiable (immediate `isConstQualified()`).
- `&_Mut`/`&_Const` of function-proto → err_mut_or_const_expr_func.
- `&_Mut` of GLOBAL in safe zone → err_safe_mut (getPrimaryDecl + IgnoreParenCasts + hasGlobalStorage).
  NOTE: `&_Const` branch has NO safe-zone-global gate (but &_Const is read-only → likely sound).

## CreateBuiltinUnaryOp BSC tail — &_Mut string-literal UB gate (SemaExpr.cpp:17124-17145) — UNPROBED

**Invariant**: `&_Mut` of a string literal (read-only memory) must be rejected
(writing through it is UB — segfault / illegal write).
**Wrapper strip used**: `Input->IgnoreParenImpCasts()` (parens + IMPLICIT casts only).
**Cases handled**:
1. Direct `&_Mut "s"` — `isa<StringLiteral>(InputIgnored)`.
2. Indirect `&_Mut *"s"` — `dyn_cast<UnaryOperator>` with `UO_Deref`, then
   `getSubExpr()->IgnoreParenImpCasts()` `isa<StringLiteral>`.
**Candidates**:
1. **Array-subscript form `&_Mut "s"[i]` — PROBED-FOLDED-into-F36 (IJOMZF)**.
   Re-confirmed live at current binary (28656aa9): `&_Mut "hello"[0]` ACCEPTED
   in pure `_Safe`, runtime SIGSEGV (write to .rodata); the deref form
   `&_Mut *"hello"` REJECTED. This is EXACTLY F36 (already filed) — same gate,
   same wrapper (ArraySubscript), same fix surface. Do NOT re-file. The whole
   string-literal gate is mined (see _probed.md 604-656, 920-923: BO_Add/comma/
   typedef/compound-literal/fn-arg all SHAPE-REJECTED or folded).
2. Explicit-cast wrapper `&_Mut *(char*)"s"` — IgnoreParenImpCasts won't strip
   an explicit cast; but the deref of a cast may not be an lvalue / may be caught
   elsewhere. Rank 2.
3. `&_Mut (*"s")` with a paren — handled (IgnoreParenImpCasts strips paren). Folds.

## err_safe_mut global gate (GetBorrowAddressOperandQualType:15356-15362) — UNPROBED

**Invariant**: in the safe zone, `&_Mut` of a GLOBAL variable is rejected
(globals have static lifetime; a mutable borrow of one in safe code breaks the
aliasing model). Uses `getPrimaryDecl(InputExpr->IgnoreParenCasts())` +
`VD->hasGlobalStorage()`.
**Wrapper strip**: `getPrimaryDecl` (:15251) recurses ParenExpr, ImplicitCast,
non-arrow MemberExpr (to base), array-subscript-of-array-ICE; returns nullptr
for ARROW MemberExpr and for bare ArraySubscript.
**Candidates**:
1. `&_Mut g_arr[i]` / `&_Mut g_s.x` / direct global — PROBED-SOUND @28656aa9.
   getPrimaryDecl recurses the array-ICE base AND the non-arrow MemberExpr base
   → all three forms correctly REJECTED with err_safe_mut. ConditionalOperator
   wrapper `&_Mut (c?g:g)` SHAPE-REJECTED (conditional of scalars is rvalue in C).
2. The `&_Const` branch (15373-15393) has NO err_safe_mut gate — but &_Const is
   read-only → sound (no aliasing-mutation hazard). Not probed (no soundness lever).

## BuildFieldReferenceExpr — member-result BSC qualifier combine (SemaExprMember.cpp:2134-2143) — UNPROBED

**Invariant**: `s.f`'s result type combines base + member qualifiers; BSC removes
`Owned` from the base before the union (a field of an owned container is not
itself owned) but KEEPS `Borrow`. `Combined = BaseQuals + MemberQuals` UNIONs.
**Asymmetry seen**: Owned removed (line 2137-2139), Borrow NOT removed. So a
`_Borrow`-qualified non-pointer struct base propagates `_Borrow` onto the member,
while an `_Owned` base does not propagate `_Owned`. Is the borrow-keep sound, or
can it manufacture a bad type (e.g. `int *_Owned` field of a `_Borrow` base
becoming `int *_Owned _Borrow`, or a borrow inherited where the member has no
lifetime)?
**Candidates**:
1. PROBED-SOUND @28656aa9. `(*bs).p` (bs:struct*_Borrow, p:int*_Owned) → type is
   `int *_Owned` (field owned kept, base borrow NOT added); `(*bs).x` (int field) →
   plain `int`. `&_Mut (*cs).x` (const struct base) → const propagates base→member,
   the &_Mut const-check (SemaExpr.cpp:15348) fires → correctly REJECTED. The
   combine is consistent with the model: owned removed from base, const inherited,
   borrow-on-non-pointer not manufactured. No soundness lever found.

## R3 CheckBSCQualTypeAssignment — the owned/borrow assignment-constraint gate (SemaBSCOwnership.cpp:482-508) — PROBED-FOLDED (no distinct root)

Dispatched from `CheckAssignmentConstraints` (SemaExpr.cpp:10315) for every BSC
assignment (init / `=` / arg-pass / return). R3E2 hunt question: does this gate
have the F79/F80 SHALLOW-PREDICATE gap (test outer `isOwnedQualified()` where it
should test a nested obligation)?

**Structure of the gate**:
- :486-487 `MayHaveOwned`/`MayHaveBorrow` from OUTER `isOwnedQualified()/isBorrowQualified()`.
- :488-492 deepens via `LHSPtr->hasOwnedFields() || RHSPtr->hasOwnedFields()`
  (and the borrow twin) — **but ONLY when BOTH sides are PointerType** (`getAs<PointerType>()`).
- :496-506 if `MayHave*`, calls `CheckOwnedQualTypeAssignment`/`CheckBorrowQualTypeAssignment`,
  which recurse pointee-by-pointee (SemaBSCOwnership.cpp:347-404 / :677-707) and
  reject on outer-qualifier mismatch or `!IsPointer` non-same-type.
- `hasOwnedFields`/`hasBorrowFields` (TypeBSC.cpp:57-99 ptr arm, :423-488 record
  arm) recurse BOTH the pointer-pointee chain AND a struct-field BFS — so
  `int*_Owned*` AND `struct{int*_Owned f;}` buried in a pointee ARE detected.

**Invariant**: an assignment whose source/dest disagree on a nested owned/borrow
obligation must be rejected (or backstopped) in pure `_Safe`.

**Result — FOLDED, no distinct root.** Tabulated {owned,borrow} × {immediate,
plain-ptr-nested 1/2/3-deep, swap, typedef-hidden, struct-field-buried, array-pointee,
arg-pass, return}. Every drop/mismatch/swap is REJECTED, either by this gate's
recursion (plain-ptr/typedef nesting at all depths: probes B,C,K,L,M,O,P,Q,W,X),
the safe-zone struct-pointer-conversion gate (different-struct launder: G,H,U), the
decl-time owned/array gate (E,R,T), or the dataflow leak detector (struct-value copy I).
Same-type controls correctly ACCEPT (D,J,V). The ONLY residual holes are the
**function-pointer-pointee** cases — `hasOwnedFields`/`hasBorrowFields` do not descend
a FunctionProtoType's params, and `CheckBSCFunctionPointerType`'s param compare uses
`isOwnedQualified()` on the whole param — which are already filed as **F74**
(ptr-to-fnptr dispatch `isFunctionPointerType()` immediate-only) and **F76**
(fnptr-param-of-fnptr canonical-pointee compare). No new DISTINCT gate/mechanism here.
Ledger: /tmp/probed_R3E2.md. Candidates: all PROBED-FOLDED-into-{F74,F76,F79,F80}.

## ActOnBinOp BSC owned-pointer-binop gate (SemaExpr.cpp:16548-16562) — PROBED-confirmed-F94

**Invariant**: a binary op with an `_Owned`-qualified pointer operand must be
rejected with ONE diagnostic; the BSC-specific owned-binop check must not
duplicate a diagnostic the standard `BuildBinOp` operand-type check already emits.
**Peers**: `DiagnoseOwnedPointerBinaryOp` (:16451, the BSC emitter — switch returns
early for EQ/NE/LT/LE/GT/GE/Assign/LAnd/LOr/Comma, emits `err_typecheck_invalid_owned_binOp`
for `default` = arithmetic ops), `BuildBinOp` (:16562, the standard path that emits
generic `err_typecheck_invalid_operands`), `CheckTemporaryVarMemoryLeak` (:16556).
**Root cause of F94**: the BSC check (:16553) runs UNCONDITIONALLY before
`BuildBinOp` (:16562). `err_typecheck_invalid_owned_binOp` (DiagnosticBSCSemaKinds.td:61)
has message text IDENTICAL to generic `err_typecheck_invalid_operands`
(DiagnosticSemaKinds.td:6943): both `"invalid operands to binary expression (%0 and %1)"`.
So when an owned arithmetic op is ALSO C-invalid (`p + q`, `p * q`, …), BOTH fire →
the user sees the same line twice. When the op is C-VALID (`p - q` ptr-diff), only
the BSC error fires → 1 line. Differential: owned `p+q`=2 errors, owned `p-q`=1,
plain/non-owned `p+q`=1.
**Candidates**:
1. **Duplicate diagnostic on C-invalid owned arithmetic — PROBED-confirmed-F94**
   (`p + q`, `p * q`, `p / q` on `_Owned` pointers → identical error twice).
2. **`CheckTemporaryVarMemoryLeak(LHS/RHS)` only on comparison ops (:16555) —
   PROBED-confirmed-LEAK (candidate, pending dedup vs F22/F47; no F# — F95 reassigned to static-local leak)**.
   The owned-temp leak check is gated to `isComparisonOp(Opc)`; an owned temporary
   as the operand of a NON-comparison op (BO_LAnd/LOr/Comma) skips it.
   `mk() && c;` (mk returns `int *_Owned`) COMPILES CLEAN and valgrind reports
   `1 allocs, 0 frees` → silent SOUNDNESS leak. probe:
   `probes/owned_temp_logical_comma_binop_leak.cbs`. Distinct CALL-SITE from
   F22 (ActOnIfStmt) / F47 (checker-internal isa) / F20 (CompoundLiteral
   expr-stmt) — fix is at the `isComparisonOp(Opc)` gate (16555). FOLD-RISK: same
   owned-temporary-not-consumed FAMILY; a comprehensive maintainer fix may cover
   all. Filing/dedup deferred to user.
3. **Owned operand reached through a wrapper (paren/cast) — UNPROBED**: the gate
   tests `LHSExpr->getType()` canonical owned-qualified; a paren/implicit-cast that
   preserves owned-qual still triggers, but one that strips it (e.g. `(int*)owned`)
   may bypass both the BSC check and leak handling (cousin of the C1 wrapper class).

## CreateBuiltinArraySubscriptExpr owned/borrow gate (SemaExpr.cpp:5940-5970) — diagnostic-spec probe
**Invariant**: subscripting an owned/borrow pointer is rejected unless `_ArrayElem`
(or -spatial-check=user); subscripting a pointer to owned-field data is rejected in
ALL modes (:5963, via `hasOwnedFields`). Owned base → `err_typecheck_invalid_owned_arrsub`;
borrow base → `err_typecheck_borrow_subscript` (:5957).
**Peers**: F94 (owned-binop dup diag), hasOwnedFields (cycle-6 array gap, dead here).
**Candidates**:
1. **line 5966 emits `..._owned_arrsub` even for a BORROW base — wrong-message (LOW) — probing**.
2. `p[i]` is C-valid so no F94-style double-emission (single BSC error). control.
3. hasOwnedFields array-gap unreachable (owned arrays forbidden). dead.

## checkRawPtrIncDecInSafeZone (SemaExpr.cpp:11897-11907) — read 2026-06-24
**Invariant**: in `_Safe`, a RAW pointer (`!isOwnedQualified && !isBorrowQualified`) must not be
`++`/`--` (`DiagnoseRawPtrIncDec`); `_Owned`/`_Borrow` operands are EXEMPTED from this raw-ptr
diagnostic (early `return true` at :11903 on canonical-type qualifier check).
**Peers**: borrowing manual rule 15 (`++`/`--`/`-`/`~`/`[]`/binary `* / % & | << >> + -` FORBIDDEN
on borrow type) vs rule 14 (`&`,`!`,`&&`,`||` ALLOWED); `err_typecheck_borrow_subscript`
(SemaExpr.cpp:5957, `[]` leg already enforced); the `&_Mut`/`&_Const`-on-fnptr checks (:15343/:15374, F114).
**Candidates (ranked)**:
1. **rule-15 borrow `++`/`--` (D1 must-reject)** — this function EXEMPTS borrow from the raw-ptr
   inc/dec diag; if no SEPARATE borrow-inc/dec check exists, `int *_Borrow p; p++;` is wrongly
   ACCEPTED (spec says forbidden). UNPROBED ⭐ (probe below).
2. **rule-15 borrow binary arith `p + 1`/`p - 1` (D1 must-reject)** — borrow pointer arithmetic;
   rule 15 forbids. Check it's rejected, not silently accepted. UNPROBED.
3. **rule-14 borrow `&p`/`!p`/`p && q` (D2 must-accept)** — controls; must compile. UNPROBED.

## CreateBuiltinArraySubscriptExpr borrow leg (SemaExpr.cpp:5945-5969) — read 2026-06-24
**Invariant**: borrow/owned-qualified pointer subscript `p[i]` is rejected (rule 10:
`err_typecheck_borrow_subscript` / `err_typecheck_invalid_owned_arrsub`) UNLESS the base is
`_ArrayElem`-qualified OR `-spatial-check=user` (documented migration escape); plus an
all-modes reject when the pointee has owned fields (:5964). Peers: rule 11 arith (probed SOUND),
rules 12 (cmp allowed) / 13 (sizeof/alignof equality guaranteed).
**Candidates**: 1. **rule-13 `sizeof(T*_Borrow)==sizeof(T*)` / `_Alignof` equality (D2 falsifiable
guarantee)** — if borrow is a fat pointer internally, the spec equality breaks. UNPROBED ⭐.
2. **rule-12 same-type-only comparison boundary** — mixed-pointee or borrow-vs-raw `==` handling. UNPROBED.
3. **rule-10 `_ArrayElem` escape interaction** with owned-fields all-modes reject. UNPROBED.
