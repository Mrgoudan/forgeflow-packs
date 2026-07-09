# BSCNullabilityCheck.cpp

Source: `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp`.

Per-expression nullability classification + CFG-based dataflow for path-sensitive narrowing. F10 (constant_p false positive), F12 (ternary narrowing), F13 (reborrow deref skip) were all here.

## State model

Three parallel dictionaries:
- `CurrStatusVD`: VarDecl* → NullabilityKind (Nullable/NonNull/Unspecified)
- `CurrStatusFP`: FieldPath → NullabilityKind (for struct field chains)
- `CurrStatusDPVD`: DerefPathVD (VarDecl + deref count) → NullabilityKind (for `*p`, `**p`)

## Functions

### `getExprPathNullability` — :307-423
**Invariant**: returns the path-sensitive nullability of an expression by recursive descent through wrappers + selected operators. Returns Unspecified for unknown shapes.
**Wrappers stripped**: ParenExpr, SafeExpr, ImplicitCastExpr.
**Operators handled in switch**: CallExpr (with `__move_to_raw` / `__take_from_raw` / array variants special-cased), ConditionalOperator (NonNull-and-NonNull → NonNull; else → Nullable; default break), CStyleCastExpr, UnaryOperator (AddrOf/AddrMut/AddrConst → NonNull; UO_Deref → checks CurrStatusDPVD; UO_AddrMutDeref/AddrConstDeref → recurse subExpr), BinaryOperator (BO_Comma / BO_Assign → recurse RHS; ALL OTHERS → break), InitListExpr (first init), DeclRefExpr, ArraySubscriptExpr, MemberExpr.
**Candidate (C3) — CONFIRMED 2026-05-19 as F18 (IJOEWJ)**: BinaryOperator switch lacks `BO_Add` / `BO_Sub` (pointer arithmetic). Pointer arithmetic on Nullable produces Unspecified → bypasses NonNull-assign check → false-negative + unsoundness. **Broadened**: same hole affects `BO_AddAssign` (`p += 1` as call arg also bypasses). Fix must enumerate all pointer-arith ops, not just `BO_Add`/`BO_Sub`.
**Remaining candidates**: `default: break` at :417-418 returns Unspecified for any unknown StmtClass. Standard-C kinds reachable in pointer position to audit: PredefinedExpr (rarely pointer), CompoundLiteralExpr (e.g. `(int *_Nullable){...}` — exotic), StmtExpr (could yield pointer; would return Unspecified → false-negative on assign).

### `NormalizeInitExpr` — :280-297
**Invariant**: strips CStyleCastExpr and CompoundLiteralExpr wrappers via `getInitializer()->IgnoreParenImpCasts()`. Looping until neither matches.
**Asymmetric with BSCOwnership.cpp**: BSCNullabilityCheck has this helper; BSCOwnership does NOT. F17 (compound literal field init) is the analogous bug in ownership that nullability avoids. If a similar normalize helper existed in BSCOwnership::VisitDeclStmt, F17 would not exist.

### `CheckInit` — :478-576
**Invariant**: recursive nullability check on init expressions, traversing nested record/array fields by FieldPath.
**Uses NormalizeInitExpr at :522**: explicitly unwraps compound literals before `dyn_cast<InitListExpr>`. Right way to do C1.
**Branches**: pointer type → check NonNull-default vs RHS nullability; record/array → recurse fields.

### `VisitBinaryOperator` — :583-646
**Invariant**: for assignment ops, derive RHS nullability and check against LHS NonNull-default. Updates path state for VD/MemberExpr LHS.
**LHS coverage**: getVarDeclFromExpr → VD-keyed update; getMemberExprFromExpr → FieldPath update. Otherwise no update.
**Candidate (C6)**: LHS forms not covered: ArraySubscriptExpr (`arr[i] = nullable_ptr`), UnaryOperator deref of pointer-to-pointer (`*pp = nullable_ptr`). Worth probing for false-negative on assign through indirection.

### `VisitUnaryOperator` — :669-678
**Invariant**: only `UO_Deref` triggers null-check. `UO_AddrMutDeref` / `UO_AddrConstDeref` intentionally skipped per comment (no actual deref happens — they yield reborrows). Documented in F13.

### `VisitArraySubscriptExpr` — :682-688
**Invariant**: `p[i]` treated as `*(p+i)` for nullability — base's nullability is checked.
**Candidate (C7)**: the index `i` isn't checked. What if `i` is `*null_int_ptr`? That deref would be on a separate CFG element (Prologue hoists?), maybe caught.

### `VisitMemberExpr` — :691-699
**Invariant**: `->` on Nullable diagnosed. `.` on a (non-pointer) record is fine.

## State propagation

- `InvalidateDerefStatusForVar(VD)` at assignment-to-VD: invalidates all `(VD, *)` deref-chain facts. Conservative — drops everything when root pointer changes.
- `InvalidateDeeperDerefStatusForPath`: invalidates deeper deref entries when shallower changes.

## Candidate status (ranked, with progress)

1. **C3 BinaryOperator switch holes** — **CONFIRMED-F18 2026-05-19** for BO_Add/BO_Sub/BO_AddAssign. Variants of the same hole are folded; no further probing of this site.
2. **C6 VisitBinaryOperator LHS coverage** — `arr[i] = nullable` **PROBED-INCONCLUSIVE** (other diag masks); `*pp = nullable` **SHAPE-REJECTED** (BSC forbids `_Borrow` of borrow-containing struct + `&_Mut` on `_Borrow`).
3. **default: break (C3)** — Standard-C exotic kinds in pointer position not in the switch (CompoundLiteralExpr pointer-yielding, VAArgExpr, etc.). **UNPROBED**, low priority — exotic constructs.

## 2026-06-29 — Chain-F REOPENED re-audit (@34883aa1) — getExprPathNullability FULL switch re-walk

Re-walked the whole switch (lines 317-433 in current source) after the file changed. The function's
ONLY laundering mechanism is uniform: any arm that `break`s OR any unhandled StmtClass → falls to
`default:` (line 427) → returns **Unspecified** (NOT Nullable). Every unhandled-kind probe is therefore
the SAME root cause as F18/F92 (one fix surface = "the switch should default-conservative to
declared/Nullable, or enumerate all pointer-producing kinds"). Findings:

- **CompoundLiteralExpr deref** `*(int*_Borrow _ArrayElem _Nullable){p}` → compiles rc=0 + runtime
  valgrind "Invalid read of size 4" + SIGSEGV (asymmetry: `*p` correctly rejected). CONFIRMED behavior
  but **FOLDS-into-F18/F92** — `CompoundLiteralExprClass` has no arm → default→Unspecified, the exact
  F92 mechanism in the SAME function. Chain F ledger already records this fold. NOT a new root cause.
- **CStyleCastExpr launder** `(int*_Borrow _ArrayElem)nullable_p` → **SHAPE-REJECTED**: Sema gate
  "cannot cast nullable pointer to nonnull type" fires BEFORE the analyzer; the `CStyleCastExpr` arm
  (line 358, `getDefNullability(getTypeAsWritten())`) is backstopped — a cast that drops `_Nullable`
  never reaches it. Sound by Sema backstop.
- **UnaryOperator inc/dec** `*++p`/`*p++` → **SHAPE-REJECTED**: BSC pointer `++`/`--` return `void`
  → `*++p` = "indirection requires pointer operand ('void' invalid)". The UO `break` (line 379) for
  non-Addr/Deref opcodes is unreachable for a deref-of-incremented-pointer.
- **CallExpr-return arm** `*getn(p)` (getn returns `_Nullable`) → **PROBED-SOUND**: rejected
  "nullable pointer cannot be dereferenced" (`getDefNullability(CE->getType())` correct).
- **DeclRefExpr arm CurrStatusVD absence** (line 400 falls to `break`→Unspecified if a `_Nullable`
  VD is absent from `CurrStatusVD`) → UNREACHABLE in `_Safe`: locals/params are pre-populated by
  `initStatus` (Chain G note); `_Nullable` globals are either non-`_Borrow` (raw-ptr deref banned in
  `_Safe`: "'*' operator is forbidden") or non-formable (`_Borrow` needs a param). PROBED-SOUND.
- **ConditionalOperator** — already PROBED-SOUND (2026-06-29 ternary-of-nullables, _probed.md).

CONCLUSION: Chain F has ONE root cause (F18/F92, the default→Unspecified switch hole). All sibling
expr-kinds fold. No distinct root cause in this function. Re-mark Chain F SATURATED @34883aa1 (the
F92 BO_Add/BO_Sub fix, when applied, should be a default-conservative rewrite covering all kinds).

## Not yet read

- IsStmtInSafeZone (line 425) — controls when diagnostics fire
- PassConditionStatusToSuccBlocks (line 773) — how condition-narrowing propagates
- mergeVD / mergeFP / mergeDPVD (line 839+) — the CFG-merge for nullability

### `getVarDeclFromExpr` / `getMemberExprFromExpr` — BSCNullabilityCheck.cpp:165-188 — READ 2026-05-20
**Invariant**: helper that unwraps expression wrappers to find VD/ME for narrowing entry resolution.
**Asymmetry**: `getVarDeclFromExpr` walks DRE / ICE / PE / **BO->LHS**; `getMemberExprFromExpr` walks ME / ICE / PE (no BO). At first glance this looks like a coverage gap for shapes like `(s.f = p) != nullptr`.
**Mitigation**: `extractDistinguishedTrackablePtr` (BSCNullCheckInfo.cpp:137-193) already resolves assignment expressions via `BO->getLHS()` in its `isAssignmentOp` branch (line 170-176), returning the LHS sub-expression directly. So by the time `SetCFGBlocksByExpr` calls these helpers, the input is already the narrowed expression (s.f), not the BO.
**Probed**: `if ((s.f = p) != nullptr) { *s.f = X; } else { *s.f = Y; }` — CORRECT (true branch narrows s.f; else fires nullable-deref diag). `/tmp/probe_assigncond_else_nullable.cbs`.
**No defect found.**

### `NullCheckInfo::operator&=` / `operator|=` — :236-336 — READ 2026-05-20
**Invariant**: combine null-check info from logical-op operands.
- `&=` (AND): union of presentCheckedExprs and nullCheckedExprs; if either side is ConstFalse → result ConstFalse, sets cleared.
- `|=` (OR): intersection of present/null sets; if either is ConstTrue → result cleared.
**No defect found** — standard set algebra matches Boolean semantics. ConstTrue/ConstFalse special-cased correctly.

## Additional reading (cycle following F39)

### `getExprPathNullability` (line 307-423)

**Invariant**: returns the nullability of an expression following its "path".
For pointer-typed expressions, dispatches on StmtClass: Paren/Safe/ICE/Call/Cond/CStyleCast/Unary/Binary/InitList/DRE/Subscript/Member. Default returns Unspecified for unhandled cases (comment claims "no-pointer type" but actually any unhandled pointer-typed stmt class also falls through).

**Status of candidates**:
- BO switch (line 371-377) handles only BO_Comma and BO_Assign — **PROBED-confirmed-F18** for BO_Add/BO_Sub gap.
- BO_AddAssign / BO_SubAssign / other compound-assignments on pointer return-value also fall through to Unspecified — **FOLDED-F18** (same root, "add cases to BO switch").
- UnaryOp switch (line 351-369) handles UO_AddrOf/AddrMut/AddrConst (returns NonNull), UO_Deref (DPVD lookup), UO_AddrMutDeref/AddrConstDeref (recurse). Doesn't handle UO_PreInc/PostInc/PreDec/PostDec — but increment/decrement on pointer returns pointer; falls through to Unspecified. Sibling of F18; folds.

### `getDerefPathVDFromExpr` (line 191-215)

**Invariant**: extracts (VarDecl, depth) from a pure deref chain. Strips IgnoreParenImpCasts then matches DeclRefExpr (depth 0) or UO_Deref (recurse depth+1).

**Reachability**: doesn't handle CStyleCastExpr — `*(cast)p` deref chain not recognized. Conservative miss (no false negative; just no state tracking).

### `initStatus` (line 808-837)

**Invariant**: pre-populates BlocksEndStatusVD/FP[entry] with Nullable for any Nullable VarDecl / Nullable FieldDecl that appears as a top-level Stmt in any non-entry/non-exit block. Bootstraps the dataflow's per-VD/per-FP tracking.

**Reachability**: only checks DeclRefExpr and MemberExpr top-level. ArraySubscriptExpr / UnaryDeref / CallExpr-results are not pre-populated. By design (these aren't trackable lvalues for per-element nullability).

## BSCNullCheckInfo.cpp

### `containsArrayAccess` (line 77-95)
**Invariant**: Returns true iff expression syntactically contains an array subscript or deref-of-additive-pointer-arith. Used by getTrackablePtr to exclude untrackable lvalues.
Recurses through MemberExpr base. Doesn't recurse through ConditionalOperator, Comma, CStyleCastExpr, etc. — but these are typically rvalues (rejected later via isLValue check).

### `getTrackablePtr` (line 100-128)
**Invariant**: returns input if trackable lvalue pointer; nullptr otherwise. Recurses through BO_Comma (RHS) and assignments (LHS).
Rejects: rvalue, non-pointer, volatile, atomic, expr containing array access.

### `extractDistinguishedTrackablePtr` (line 137-195)
**Invariant**: extracts a trackable pointer from a condition, marking nullness/non-nullness.
Handles BO_Comma (recurse RHS), assignment (LHS first, RHS fallback), equality (with nullExpr on one side). Doesn't handle ConditionalOperator at top — but ternary as condition is rare.

### `NullCheckInfo::init`, `&=`, `|=`
**Invariants**: handle `!`, `&&`, `||` for null-check composition. Union for `&&`, intersection for `||`. Triviality-state machine for ConstTrue/ConstFalse short-circuit.

**Status**: no obvious new bugs identified in this cycle; mainly consistent with F12's narrowing-not-propagated theme.

## TransferFunctions::VisitBinaryOperator (BSCNullabilityCheck.cpp:583-646) — PROBED-confirmed-F48 (HIGH)

**Invariant**: when a pointer assignment `BO(LHS, RHS)` is visited, validate the assignment (Nullable→Nonnull rejected) and update the LHS's narrowing state to reflect the new value.

**Peers**: `CheckInit` (line 478) does the symmetric job at DeclStmt time. `VisitUnaryOperator` (line 669) is the consumer of CurrStatusVD updates.

**Filed candidate (F48)**: `BO->isAssignmentOp()` at line 584 is true for BOTH `=` AND compound assigns (`+=`, `-=`, etc.). The state update at line 608-619 uses `RHSKind = getExprPathNullability(BO->getRHS())`, which is meaningless for compound pointer assigns because `getRHS()` is the integer operand. Result: `CurrStatusVD[VD] = Unspecified` overwrites the prior Nullable, and subsequent deref accepts without diag → runtime Invalid read.

**Distinction from F18**: F18 fixes `getExprPathNullability`'s BinaryOperator switch (line 371-377) for queries on `p += 1` AS AN EXPRESSION VALUE. F48 fixes the STATE-UPDATE side of `VisitBinaryOperator` for subsequent uses in separate statements.

**Folded shapes**: `p -= 1` (BO_SubAssign), any compound op with integer RHS on a pointer.

**Fix surface**: line 619 — distinguish simple vs compound assign. For compound, preserve `CurrStatusVD[VD]` (or set conservatively to Nullable). Rough:
```cpp
bool IsCompound = BO->getOpcode() != BO_Assign;
if (VarDecl *VD = getVarDeclFromExpr(LHS)) {
  if (IsCompound) {
    // Compound assign of pointer with integer RHS: preserve prior narrowing.
  } else if (LHSKind == NullabilityKind::NonNull) { ... }
    else if (CurrStatusVD.count(VD)) CurrStatusVD[VD] = RHSKind;
  InvalidateDerefStatusForVar(VD);
}
```

## Switch-case fallthrough narrowing audit (cycle 2026-05-21, no defect)

**Audit hypothesis**: switch-case fallthrough joins may produce a state hole where narrowing from one case bleeds into another (false negative) or fails to widen back to Nullable (false positive).

**Code-reading findings**:
- `PassConditionStatusToSuccBlocks` (line 773-803) gated on `block->succ_size() == 2` (line 910). Switch blocks have N+1 successors so this NEVER runs for them — but switch condition is always an integer, so `NullCheckInfo(CondExpr)` would extract nothing anyway. Defensive but moot.
- `BSCNullabilityCheck.cpp` has zero `SwitchStmt`/`CaseStmt`/`DefaultStmt` references. All switch-case behavior is via generic CFG merge.
- `mergeVD`/`mergeFP`/`mergeDPVD` (line 839-887) all do Nullable-over-NonNull conservatively. For fallthrough, case 2's preds include switch-block (pre-narrowing state) AND case-1-body-exit (post-narrowing state); merge correctly takes Nullable.

**Probed shapes** (8, all PROBED-clean):
1. `_Safe int f(int *_Borrow _Nullable p)` with bare `return *p;` — diag fires correctly via type-default Nullable.
2. switch-case fallthrough with case-1 reassign-to-Nullable then case-2 `*p` — diag fires.
3. switch-case fallthrough with case-1 `if(!p)break;` then case-2 `*p` — diag fires (correct conservative since case 2 reachable without case 1).
4. switch with pre-switch `if(!p)return;` narrowing — narrowing preserved into all cases, no diag.
5. `switch(p==nullptr)` boolean — analyzer ignores switch condition; bare `*p` in case 1 still gets type-default Nullable diag.
6. do-while body narrow `if(!p)return;` then `*p` — correct, no diag.
7. `if(!p)goto err;` then `*p;` — narrowing through goto preserved.
8. `for(;p;){*p;}` — narrowing through for-cond preserved.

**Conclusion**: switch-case fallthrough/condition narrowing surface is sound. The `succ_size()==2` gate excludes switch terminators from condition-based narrowing, but the underlying merge-prefers-Nullable behavior makes the analyzer correctly conservative across all fallthrough shapes tested. No new defect class beyond F26 (mergeDPVD asymmetric meet) and F33 (VisitMEForFieldPath UnaryOperator hole).

**Folded shapes** (NOT separate root causes):
- All case-fallthrough variants: same `mergeVD/FP/DPVD` code path that's already audited.
- switch boolean condition: predates this audit; analyzer relies on type-default Nullable.

## TransferFunctions::VisitCallExpr (BSCNullabilityCheck.cpp:649-663) — PROBED-confirmed-F49 (HIGH)

**Invariant**: when a `CallExpr` is visited, for every parameter declared `_Nonnull`, verify the corresponding argument is not `_Nullable`.

**Peers**: BorrowCheck `ActionExtract::VisitCallExpr` (line 469-478) — also iterates `CE->arguments()` but skips `getCallee()`. Ownership `TransferFunctions::VisitCallExpr` (BSCOwnership.cpp:2208) iterates args without consulting callee param types.

**Filed candidate (F49)**: the loop is gated on `if (FunctionDecl *FD = CE->getDirectCallee())`. For indirect calls (function pointer, callback, dispatch table), `getDirectCallee()` returns null, the if-body is skipped, and the per-param nullability check never runs.

**Soundness consequence (F49 IJOUEG)**: `_Safe int (*fp)(int *_Borrow) = taker; fp(nullable_q);` compiles clean; valgrind reports Invalid read inside taker's `*p` deref.

**Fix surface**: when `getDirectCallee()` returns null, fall back to the callee expression's type. The CallExpr's callee has FunctionPointerType → FunctionProtoType from which to read `getParamType(i)`:
```cpp
const FunctionProtoType *FPT = nullptr;
if (FunctionDecl *FD = CE->getDirectCallee())
  FPT = FD->getType()->getAs<FunctionProtoType>();
else if (const Expr *Callee = CE->getCallee()) {
  QualType CT = Callee->getType();
  if (CT->isFunctionPointerType())
    CT = CT->getPointeeType();
  FPT = CT->getAs<FunctionProtoType>();
}
if (FPT) for (i in params) { check using FPT->getParamType(i) }
```

**Cross-references**: F24 (BorrowCheck callee-not-visited); F18/F48 (other nullability gaps); F11 (BO_Comma).

## 2026-05-21 Explorer cycle on BSCNullCheckInfo.cpp condition-walk + `SetCFGBlocksByExpr` / `BlocksConditionStatusDPVD` (no-new-pattern)

**Audit target**: per hint, the suspected single-pair-per-edge overwrite in `BSCNullabilityCheck.cpp:738-741` and the propagator `BSCNullabilityCheck.cpp:970-975`; plus `NullCheckInfo::init` recursion (`BSCNullCheckInfo.cpp:198-225`), `&=`/`|=` set algebra (236-334), `invert` (227-234), `extractDistinguishedTrackablePtr` (137-195).

**Reading findings** (these are STRUCTURAL observations, not new bugs):

- `BlocksConditionStatusDPVD[block][pred]` is `std::pair<DerefPathVD, NullabilityKind>` — a SINGLE pair per edge. **Asymmetric with `BlocksConditionStatusVD[block][pred]` (a `StatusVD = std::map<VarDecl*, Nullability>`)** and `BlocksConditionStatusFP[block][pred]` (same per-key map). At first glance this looks like overwrite-on-multi-track. **In practice it does NOT manifest because CFG splits `&&`/`||` into separate blocks, and `block->getLastCondition()` returns a single leaf. Each leaf has at most ONE trackable deref in `extractDistinguishedTrackablePtr`.** Probed `if (*pp && *qq)` with `int *_Borrow _Nullable *_Owned pp/qq` — both narrowings applied correctly in the body.
- `init()`'s `UO_LNot` over `&&` recursion (`!(p && q)`) — would WRONGLY conclude `null={p, q}` (both null) after `invert()` of the `&&`-union. This is over-strong inference. BUT: the CFG already splits `&&` regardless of outer `!`, so `init()` never sees a logical operator at the level of the per-block condition. Probed `if (!(p && q)) return 0; return *p + *q;` — clean (CFG handles it).
- `containsArrayAccess` doesn't recurse through `ConditionalOperator`/`BO_Comma`/`CompoundLiteralExpr` — only `MemberExpr` base. Probably moot because the outer `getTrackablePtr` rejects rvalues, and these wrappers commonly produce rvalues.
- `extractDistinguishedTrackablePtr` BO_EQ XNOR check returns `nullptr` if both sides are isNullExpr or both are non-null-expr. Safe.
- `extractDistinguishedTrackablePtr` doesn't handle `ConditionalOperator` at top level — `if (cond ? p : q)` → no narrowing. Conservative.
- `getDerefPathVDFromExpr` accepts only DRE-rooted deref chains (no MemberExpr base) — so `*s.f` (deref of struct field of double-pointer) is untracked in DPVD. Conservative (would not enable narrowing through this shape but doesn't introduce false negative).
- `MaybeSetNull` in `BSCOwnership.cpp:2581-2602` also uses `NullCheckInfo`, with cur->succ_begin()[0/1] resolving true/false branch. If `cur->getLastCondition()` returns a logical op (impossible after CFG split), the `init` recursion's `!(p && q)` over-strong inference would propagate to ownership — but CFG split prevents it. Probed `if ((1, (a && b)))` (comma + `&&`) — also clean; CFG splits inner `&&`.

**Probed shapes** (8 hard-budget; all PROBED-clean):
1. `_Safe int test(p, q): if (!(p && q)) return 0; return *p + *q;` (`_Borrow _Nullable`) — clean.
2. `_Safe int test(p, q): return (p && q) ? *p + *q : 0;` — clean.
3. Double-deref AND `if (*pp && *qq) { **pp + **qq }` with `_Borrow _Nullable *_Owned` — clean, both narrowings preserved (the single-pair-per-edge overwrite hypothesis disproven for this shape).
4. Field narrow `if (s.f != nullptr) return *s.f;` — clean.
5. Comma RHS narrow `if ((q, p)) { *p }` — clean.
6. Comma + assign in `&&` `if ((p = foo(p), p) && q) { *p + *q }` — clean.
7. Comma with logop in ownership `if ((1, (a && b))) ... else ...` — clean (no false-positive leak).
8. For-step with logop `for (;;(i++, (a && b)))` — clean.

**Conclusion**: the `BlocksConditionStatusDPVD` single-pair-per-edge structure is suspicious but currently sound because the upstream CFG splitting and `extractDistinguishedTrackablePtr` single-extract behavior make the multi-track scenario unreachable from in-scope BSC condition shapes. The asymmetry with `BlocksConditionStatusVD` (multi-key map) is an interface inconsistency, not a defect — would matter only if either (a) CFG-split was suppressed for some condition shape, or (b) `extractDistinguishedTrackablePtr` returned multiple deref-chains. Neither holds for in-scope constructs.

**No new root cause filed.** All sites here either fold into existing finds (F18 BO_Add hole, F26 mergeDPVD asymmetric meet, F48 compound-assign LHS update, F49 indirect-call gap, F50 isAssignmentOp over-broad) or are conservatively sound.

**Recommendation for next session**: pivot to `BSCNullabilityCheck.cpp:479-577` `CheckInit`'s array-fill recursion at line 567-573 — the implicit-trailing-element check uses `Init=nullptr` which fires `NonnullInitByDefault` only when `FindNonnull(ElemTy)` is true. For struct-of-struct nesting with nonnull-pointer fields nested several layers, the `FindNonnull` recursion may not reach. Worth a structured probe. Also: `getInitializedFieldInUnion()` at line 534 may return nullptr for empty union initializers — defensive null check absent.

## 2026-05-21 Explorer cycle — CheckInit (line 478-576) + getInitializedFieldInUnion (line 534, 544)

### `CheckInit` (line 478-576) — UNPROBED→PROBING

**Invariant**: recursively check that every nonnull pointer field/element of a declaration's type has a non-null initializer; if any leaf is implicitly zero-init AND `FindNonnull(QT)` is true, emit `NonnullInitByDefault`.

**Peers**:
- `FindNonnull` (line 251-277) — does the symmetric "would default-init be unsafe?" check; recurses through array element + record fields.
- `getDefNullability` (line 234-249) — pointer-level nullability extraction.
- `NormalizeInitExpr` (line 280-297) — wrapper-stripper for init RHS.
- `VisitDeclStmt` (line 467-475) — sole caller; one entry per VarDecl.

**Candidates** (ranked):
1. **getInitializedFieldInUnion null-deref (line 537-540)** — SHAPE-REJECTED (2026-06-17): empty-braces `{}` yields the FIRST declared field (not null) so FD->getType() is safe; the uninit-nonnull case is intercepted upstream by NonnullInitByDefault before CheckInit recurses (probes u1-u7). For `union U u = {};` (empty designated-less init list), `ILE->getInitializedFieldInUnion()` returns null. Code uses FD->getType() / FieldsToProcess.push_back(null) immediately — null deref / null entry in vector → likely crash inside CheckInit recursion when the null FD is processed. Even higher signal: if union has only nonnull-pointer members, the `if (NumInits == 0)` branch is the natural way to write `{}` and would hit this exact path.
2. **`FindNonnull` recursion depth on struct-of-struct-of-struct** — UNPROBED. `FindNonnull` does recurse through nested records (line 269-273), so this is probably sound. Lower priority.
3. **`FindNonnull` doesn't recurse on union member types?** — Re-read line 266-274: `if (CanQT->isRecordType()) { for (FieldDecl *FD : RD->fields()) ... }`. For a union, `fields()` returns all members; FindNonnull will return true if ANY member is nonnull. This means: declaring `union { int *_Nonnull p; int *_Nullable q; } u;` with NO init would fire NonnullInitByDefault, but that's a false positive (the union could be intended for `q`). However, no init means uninitialized memory — the FALSE-POSITIVE direction is conservative; not a soundness issue. The OPPOSITE direction: `union {int *_Nonnull p;}` with `union U u = {};` should fire NonnullInitByDefault on the nonnull field. UNPROBED — could be a false negative if the union recursion silently returns.

## 2026-05-21 Explorer cycle on PassConditionStatusToSuccBlocks → getVarDeclFromExpr/getMemberExprFromExpr/getDerefPathVDFromExpr (CONFIRMED-new)

### `getVarDeclFromExpr` / `getMemberExprFromExpr` / `getDerefPathVDFromExpr` (lines 164-215) — SafeExpr-strip gap

**Invariant**: these three helpers walk through expression wrappers to find the underlying VarDecl / MemberExpr / DerefPathVD. They explicitly handle ImplicitCastExpr, ParenExpr (and `getDerefPathVDFromExpr` uses `IgnoreParenImpCasts`), but **none of them strip `SafeExpr`** (the BSC-specific AST node produced by `_Safe(...)` / `_Unsafe(...)` expression-form wrappers).

**Peers**: F62 already flagged the same SafeExpr-strip gap in `IgnoreParenCasts`'s call site at `CheckMoveVarMemoryLeak` (SemaBSCOwnership.cpp). That filing's blast-radius list named several other Sema/Ownership predicates but did NOT include the nullability narrowing flow.

**Candidate (C1 SafeExpr-strip extension into nullability narrowing flow)** — **CONFIRMED-new**:
- Shape `if (_Safe(p) == nullptr) return; *p;` and `if (_Safe(p)) { *p; }` produce FALSE-POSITIVE `error: nullable pointer cannot be dereferenced` even though p is post-narrowing NonNull.
- Mechanism: `extractDistinguishedTrackablePtr` (BSCNullCheckInfo.cpp:137-195) extracts the trackable pointer from the condition; `getTrackablePtr` (BSCNullCheckInfo.cpp:100-128) treats `SafeExpr(p)` as a trackable lvalue (returns the SafeExpr-wrapped expression itself). Then `PassConditionStatusToSuccBlocks` (line 773-803) → `SetCFGBlocksByExpr` (line 729-769) calls the three helpers above on `SafeExpr(p)`. None strip SafeExpr, so VD lookup, ME lookup, and DPVD lookup all fail. The TRUE/FALSE successor blocks get no per-VD narrowing entries. The post-return `*p` deref sees p as Nullable (initStatus default) and the analyzer fires.
- Asymmetry baseline: same source without the `_Safe(...)` wrapper (`if (p == nullptr) return; *p;`) compiles clean.
- Repro: `/tmp/explorer_repro.YXctp7.cbs`. Baseline: `/tmp/explorer_baseline.Os6sGe.cbs`.
- Folded sibling: bare condition `if (_Safe(p)) { *p; }` (no equality op) — `/tmp/explorer_probe.d4y8wA.cbs`. Same root cause; both shapes fold into the same fix.

**Distinction from F62**: F62 is `CheckMoveVarMemoryLeak` in `clang/lib/Sema/BSC/SemaBSCOwnership.cpp:569-581` — a Sema-time move-through-borrow predicate, soundness FN (silent double-free). Mine is the dataflow narrowing flow in `BSCNullabilityCheck.cpp` + `BSCNullCheckInfo.cpp` — a CFG-based path-sensitive check, precision FP (reject valid code). Different file, different function, different invariant, different defect direction. F62's per-site peel fix would not close this; F62's cross-cutting `IgnoreExpr.h` extension WOULD close both (and is the preferred fix).

**Severity**: MEDIUM. False positive on canonical idiom: `_Safe(...)` is a standard BSC expression wrapper and may appear around any condition (e.g., transitioning a sub-expression's safety zone). The workaround is clumsy (rewrite to `_Safe int v = _Safe(*p); ...` extracting first, which loses the narrowing-flow purpose).

**Fix surface**:
1. Per-site peel in BSCNullabilityCheck.cpp lines 164, 178, 191: add an `if (auto *SE = dyn_cast<SafeExpr>(E)) return get*FromExpr(SE->getSubExpr());` arm to each.
2. Or extend `getTrackablePtr` in BSCNullCheckInfo.cpp:100 to strip SafeExpr at the start.
3. Or the cross-cutting `IgnoreExpr.h` SafeExpr-strip fix (option preferred per F62's writeup).

### Probe results — CheckInit / FindNonnull (2026-05-21)

**Candidate 1 (getInitializedFieldInUnion null deref)**: PROBED-shape-rejected. Sema's `CheckListElementTypes` (SemaInit.cpp:1296-1308) sets InitializedFieldInUnion to the first non-bitfield member for an empty `{}` initializer; the field pointer is never null in practice for non-designated empty init lists. Designated-overwrite paths (line 2704) also re-set immediately. Couldn't construct an in-scope BSC source that reaches CheckInit with `getInitializedFieldInUnion() == nullptr`.

**Candidate 2 (FAM with _Nonnull element type)**: **CONFIRMED-new** — `FindNonnull` at line 262-265 doesn't distinguish IncompleteArrayType from ConstantArrayType. For partial-init of an outer struct containing a FAM with _Nonnull-bearing element type, the per-field recursion produces a spurious "type contains nonnull pointer must be properly initialized" diagnostic. A FAM has zero in-place elements; there's nothing to initialize.

- Repro: `/tmp/explorer_probe.5T6EP3.cbs`
- Baseline (same shape, _Nullable element): `/tmp/explorer_probe.r8F58P.cbs`
- Defect class: C1 (Ignore-asymmetry / type-discrimination missing)
- Fix surface: `FindNonnull` line 262 — guard `isArrayType()` recursion behind `isa<ConstantArrayType>(QT->getAsArrayTypeUnsafe())` so that IncompleteArrayType (FAM) returns false. Alternatively, in `CheckInit` line 484, skip the early-return diagnostic when the type is `IncompleteArrayType` (no elements means no init required).

**Candidate 3 (deeply-nested struct-of-struct)**: PROBED-clean. Recursion through nested records correctly reaches deep _Nonnull. Sound.

## getExprPathNullability — ArraySubscriptExprClass case (:397-401) — INTENTIONAL, do not probe
**Invariant**: a subscript expression's path nullability is its STATIC element-type
nullability, never flow-narrowed. Code comment: "Builtin array elements cannot have
independent path-sensitive state." So `if (pp[i] != nullptr) *pp[i];` false-positives
(even constant index) while the `*pp` deref-chain form narrows correctly via
`getDerefPathVDFromExpr` (UO_Deref-only, :205-210). This deref-narrows / subscript-doesn't
asymmetry is BY DESIGN (element aliasing). Workaround: bind `e = pp[0]; if (e) *e;`.
Probed 2026-05-29 (SHAPE-REJECTED, not filed); folds with _probed:373. Sibling design:
`containsArrayAccess` filters subscript bases out of trackable extraction.

## PassConditionStatusToSuccBlocks / SetCFGBlocksByExpr (BSCNullabilityCheck.cpp:730-804) — 2026-05-29
**Invariant**: applies condition narrowing to CFG successors assuming succ[0]=true-branch,
succ[1]=false-branch (line 791-793). present-checked exprs → NonNull on True/Nullable on False;
null-checked → swapped. NullCheckInfo(CondExpr) computes the facts (F70 invert lives there).
**Candidates**: 1. succ-order assumption wrong for some two-successor terminator (do-while?
ternary-as-cond?) → narrowing applied to wrong branch → FN(null deref)/FP. if/for/while CLEAN
(_probed:440). 2. duplicated `FieldPath FP; VisitMEForFieldPath` (:756 + :761 shadow) — benign redundancy.
3. F70 invert manifestation in the swapped null-checked path.
**Probe outcome (2026-05-29): PROBED-SOUND.** succ-order assumption holds for do-while too: while-body narrows (clean); do-while body-deref BEFORE cond correctly rejected (not narrowed, body precedes cond); post-loop null deref rejected. Narrowing applied to correct branches across if/for/while/do-while. The duplicated FieldPath (#2) is benign. No bug.

## mergeVD / mergeFP / mergeDPVD (BSCNullabilityCheck.cpp:844-892) + worklist condition-injection (:1193-1251) — 2026-05-29 MERGE-CHAIN deep audit (NO-NEW)
**Invariant**: at a CFG join, the per-VD/per-FieldPath/per-DerefPath nullability is the MEET
(Nullable-over-NonNull) of every predecessor's end-status (after per-edge condition narrowing is
injected). An absent key on a predecessor MUST be read as the type-default (Nullable), never "no fact".
**Peers**: initStatus (:813-842, pre-populates entry status), SetCFGBlocksByExpr (:734-774, produces
condition-status), getExprPathNullability (:312-424, consumer of merged state for the deref diag).
**Chain map (Mode-2)**:
 - PRODUCER: runOnBlock→PassConditionStatusToSuccBlocks→SetCFGBlocksByExpr writes
   BlocksConditionStatus{VD,FP}[succ][block] (MAPS) and ...DPVD[succ][block] (SINGLE pair).
 - INJECTOR+MERGE: runNullabilityCheck worklist (:1200-1232) copies each pred's end-status, overwrites
   the condition-narrowed key(s), then `valX = mergeX(valX, predValX)` across preds.
 - CONSUMER: getExprPathNullability DeclRef(:390-398)/Member(:408-420)/UO_Deref(:360-369) — note all
   three treat "key absent from CurrStatus" as fall-through to getDefNullability (Nullable for raw/
   _Nullable) for VD/FP/UO_Deref-with-DP, but UO_Deref WITHOUT a DP entry returns getDefNullability of
   the deref TYPE (which is the declared element nullability) — this is where F26's absent-DPVD leaks.
**Findings**: mergeVD/mergeFP carry the SAME absence-asymmetry code as mergeDPVD (the F26 bug) but are
SOUND because initStatus pre-populates VD/FP keys with Nullable and those keys are NEVER erased
(InvalidateDeref* only touches DPVD), so every predecessor always HAS the key → the absence branch
(:854/869/887 `else statusA[VD]=NK`) never fires for VD/FP. DPVD is the sole victim (F26). meet(NonNull,
Unspecified)=NonNull is a real lattice weakness but unreachable for a pointer without first hitting
F18/F48 (getDefNullability never yields Unspecified for a pointer).
**Candidates** (all resolved):
1. VD/FP absent-key merge — **SHAPE-UNREACHABLE** (pre-population + no-erase). Confirms F26 distinction.
2. DPVD loop back-edge / depth-divergence / assignment-created entry / both-preds-present — all
   **SOUND or FOLD-F26** (both-preds meet correctly yields Nullable; F26 is strictly the absent-key case).
3. meet(NonNull,Unspecified) lattice — **FOLD-F18** (Unspecified source only via F18 hole).
**No new root cause.** The merge family's only soundness hole at commit 28656aa9 is F26 (mergeDPVD
absent DerefPath). 8 probes logged in _probed.md 2026-05-29.

## Chain J — `CheckInit` (BSCNullabilityCheck.cpp:483) vs `CheckGlobalInit` (SemaDecl.cpp:14812) — union `!Init` early-return asymmetry — 2026-05-30 (Explorer)

**Invariant**: the two peers must agree on whether a NESTED-or-TOP-LEVEL initializer with
nullable/nonnull fields is accepted. `CheckInit` runs per-function-CFG (local vars);
`CheckGlobalInit` runs at Sema decl-finalize (file-scope/global vars). Same source shape →
same verdict.

**Peers**:
- `CheckInit` (BSCNullabilityCheck.cpp:483-581) — function-local path.
- `CheckGlobalInit` (SemaDecl.cpp:14812-14895) — global/file-scope path.
- shared helpers: `FindNonnull` (BSCNullabilityCheck.cpp:256 — iterates ALL union fields),
  `getDefNullability`, `NormalizeInitExpr`. NOTE: global uses `GetExprNK` for the pointer-RHS
  arm; local uses `getExprPathNullability` — DIVERGENT helper, separate sub-candidate.

**Fresh fix (09074459 "fix null check of union init")**: added a union-narrowing to the `!Init`
early-return branch of `CheckGlobalInit` (SemaDecl.cpp:14816-14822): for a default-initialized
union, `QT = RD->field_begin()->getType()` (only the FIRST/active member is checked) BEFORE
`FindNonnull`. The SAME fix was applied to the `NumInits==0` ILE branch of BOTH peers
(:14852/:537). BUT the `!Init` early-return branch of the LOCAL peer `CheckInit` (:488-493)
was NOT given the union narrowing — it still calls `FindNonnull(QT)` on the WHOLE union type,
which iterates ALL fields (FindNonnull:274 `for (FieldDecl *FD : RD->fields())`).

**Asymmetry**: a default-initialized (no initializer / `= {}`) union whose FIRST field is a
non-nonnull pointer but a LATER field is `_Nonnull`:
- GLOBAL: narrows to first field → first field not nonnull → NO diag (correct: default-init
  activates the first member, which is fine).
- LOCAL: `FindNonnull(whole-union)` finds the later `_Nonnull` field → emits
  `err_nonnull_init_by_default` → **FALSE POSITIVE** (rejects valid code).

The `!Init` branch IS reachable for the local path: a function-local `union U u;` (declared,
no init) or `union U u = {};` reaches `CheckInit` with `!Init` (declared-uninit) or via the
`NumInits==0` path (handled correctly) — the declared-uninit `union U u;` is the trigger.

**Candidates**:
1. **Local `!Init` union over-diagnoses non-first nonnull field** — `CheckInit` :488-493 missing
   the union-first-field narrowing that the fix added to `CheckGlobalInit` :14816-14822 and to
   both `NumInits==0` branches. FALSE POSITIVE. RANK: HIGH (clean global-accepts/local-rejects
   differential; the fix author narrowed 3 of 4 sites and missed this one). — PROBING.
2. **Global vs local pointer-RHS helper divergence** (`GetExprNK` vs `getExprPathNullability`) —
   global may miss a Nullable→Nonnull case the local catches (F18-family interaction). RANK: MED.
3. **Nested union field with no field-init inside a struct global** — the `NumInits==0` recursion
   is symmetric now (both narrow); likely FOLD. RANK: LOW.

## Chain N branch (2) — `GetExprNK` (SemaDecl.cpp:14714) vs `getExprPathNullability` (BSCNullabilityCheck.cpp:312) pointer-RHS helper divergence — 2026-05-30 (Explorer) — UNPROBED→PROBING

**Invariant**: the pointer-init arm of both clones must classify a pointer RHS's nullability identically. LOCAL `CheckInit` :498 calls `getExprPathNullability(Init)`; GLOBAL `CheckGlobalInit` :14830 calls `GetExprNK(Init)`. For a `_Nonnull` LHS, a `Nullable` RHS must diag in BOTH. Same source → same verdict.

**Side-by-side diff (the two switches)**:
| switch case | `getExprPathNullability` (local) | `GetExprNK` (global) |
|-------------|----------------------------------|----------------------|
| CallExpr builtin set | 4: `__move_to_raw`, `__take_from_raw`, **`__move_array_to_raw`, `__take_array_from_raw`** (:331-336) | **only 2**: `__move_to_raw`, `__take_from_raw` (:14733-14734) |
| UO_Deref | path-sensitive `CurrStatusDPVD` lookup, fall back to def (:360-369) | def-nullability only (:14759-14760) — no flow state |
| DeclRefExpr `Nullable` var | consults `CurrStatusVD[VD]` flow-narrowed value (:395-396) | stops at NonNull check; Nullable var → falls through → Unspecified (:14781-14787) |
| MemberExpr `Nullable` field | consults `CurrStatusFP` (:413-417) | stops at NonNull; Nullable field → Unspecified (:14797-14803) |

**Reachability of each at GLOBAL/file scope**: globals require a constant-expression initializer, so flow-state cases (UO_Deref/DeclRef-narrowed/Member-narrowed) cannot appear in a global init — those divergences are global-unreachable (the global path having NO flow state is correct for constant-init). BUT the **CallExpr builtin-set** divergence is reachable: a `__move_to_raw`/`__take_from_raw` wrapper recurses identically in both, but the ARRAY variants `__move_array_to_raw`/`__take_array_from_raw` are NOT in `GetExprNK`'s set → global returns `getDefNullability(CE->getType())` (the raw-result-pointer's static nullability) instead of recursing into the arg. STATIC-LOCAL path uses `getExprPathNullability` (4-builtin) so it recurses.

**Candidates**:
1. **Array-transfer builtin RHS at global pointer-init** — divergent builtin set. Direction: if the array-transfer-builtin RESULT type is `_Nonnull`-default but the wrapped arg is `_Nullable`, local recurses (catches Nullable) and global stops at result-def. Need to find which direction is the FN (global wrongly accepts). RANK: HIGH if reachable. — **PROBED-SOUND/benign 2026-05-30**: `handleBSCRawTransferBuiltin` (SemaChecking.cpp:266-269) propagates the arg's nullability ONTO the result type, so `getDefNullability(CE->getType())` (the global fallback) already equals the local recursion → the 4-builtin-vs-2-builtin divergence is a no-op. Globals also can't take the non-constant raw arg. `/tmp/explorer_probe.CN7EPL.cbs`.
2. **Whether array-transfer builtins are even callable in a constant-init / `_Safe` decl context** — these are `__move_array_to_raw` etc. (raw-pointer transfer); may be `_Unsafe`-only or non-constant → SHAPE-REJECTED risk. — **CONFIRMED non-constant** (raw-arg → global-unreachable). The other branch-(2) flow-state cases (UO_Deref / DeclRef-narrowed / Member-narrowed) are likewise global-unreachable (globals require constant init, which carries no flow state) — the global path's absence of flow state is CORRECT, not a parity gap.

**RESOLUTION (2026-05-30, Explorer — NO-NEW)**: Chain N branches (2)(3)(4) are SOUND at 28656aa9.
- (2) helper divergence: flow-state cases global-unreachable; builtin-set divergence benign (result-type nullability propagation).
- (3) `NumInits==0` empty-ILE union `= {}`: SYMMETRIC (both narrow to active union field — 09074459 fixed both). Clean at both scopes.
- (4) array recursion `getAsArrayTypeUnsafe` vs `getAsArrayType`: SOUND — element `_Nonnull` read identically for `const int *_Nonnull arr[N]`; both diag the trailing-default-init.
- KEY structural finding: `VD->getInit()` delivers the SEMANTIC-form ILE to the LOCAL `CheckInit` (verified via -ast-dump on in-order, out-of-order, and nested designators — no `DesignatedInitExpr` survives), so CheckInit's syntactic-`getInit(idx)` indexing is always correct. The `getSemanticForm()` call unique to CheckGlobalInit (:14841-14844) is defensive/redundant, NOT a divergence.
- The ONLY live parity gap is branch (1) = **F78** (`!Init` union-narrowing missing in CheckInit :488-493). The nested-via-struct-recursion manifestation (`struct{int a; union{int first; int*_Nonnull nn;}u;} = {.a=1}` → static-local FP, file-scope clean) is a **FOLD of F78** (same one-line fix surface; only the entry path into the `!Init` branch differs).
- **Chain N → recommend SATURATED @ 28656aa9** (F78 lone filing).

## E1 composition — nullability × ownership cross-analyzer after move/null (2026-05-30 Explorer, NO-NEW)

**Surface**: an `_Owned _Nonnull` (or `_Owned _Nullable`) pointer is tracked by BOTH the
nullability checker (`CurrStatusVD`/`CurrStatusFP`, never cleared on a move — nullability has NO
move concept; grep confirms zero `move`/`consume` handling, only `__move_to_raw` as a value-query
special-case) AND ownership (`OPSStatus`/`SStatus` bit-lattice). Question: after a shared mutation
(move-out, nullptr-assign, field-move) do the two states stay jointly sound?

**Structural facts found (the two analyzers' disagreeing-state machinery):**
- Nullability `CurrStatusVD[p]` stays `NonNull` forever after a move (no clear). So nullability is
  NEVER load-bearing in a move-out scenario — it always believes the moved pointer is still NonNull.
- Ownership is the SOLE guard for use-after-move, and it IS robust: a plain `_Owned` pointer dereffed
  after a move is rejected even inside `if(p){*p}` / `if(p!=null){*p}` narrowed branches (P15/P15b).
- Ownership `MaybeSetNull` (BSCOwnership.cpp:2580-2601) is the ONE place ownership consults the
  nullability condition-info (`NullCheckInfo(Cond)`): on the false branch of `if(p)` it `setToNull`s
  the present-checked exprs. `setToNull` (BSCOwnership.cpp:792-805) does `resetAll(VD)` → it RESETS
  the Moved bit, resurrecting a moved pointer to `Null`.
- `checkOPSUse` (BSCOwnership.cpp:925-928): a `Null` status produces a use-of-moved diag ONLY when
  `!OPSAllOwnedFields[VD].empty()` (struct-with-owned-fields ptr). For a PLAIN `_Owned` pointer
  (`OPSAllOwnedFields` empty) a `Null` status falls through ALL branches → no diagnostic. This is the
  composition's weakest cell.

**State table (move/nullptr/field × nullability-state × ownership-state → verdict):**
| op | nullability state | ownership state | deref/reuse verdict | sound? |
|----|-------------------|-----------------|---------------------|--------|
| `sink(p)` move plain `_Owned _Nonnull`, then `*p` | NonNull (stale) | Moved | REJECT (use of moved) | yes (ownership independent) |
| move, then `*p` inside `if(p){}` / `if(p!=null){}` | NonNull narrowed | Moved | REJECT (use of moved) | yes |
| move, then `if(p)` / `if(p!=null)` CONDITION only (no deref) | NonNull narrowed | Moved | ACCEPT (condition-use bypasses checkOPSUse) | benign — reads dangling-but-non-null BITS for a compare, no deref enabled |
| `p = nullptr` on `_Owned _Nonnull` | — | — | REJECT (`nonnull cannot be assigned by nullable`) | yes |
| `src(_Nullable owned) → p(_Nonnull owned)` slot | — | — | REJECT (same) | yes |
| `*s.f` after whole-struct move `take(s)` of `_Nonnull` field | NonNull (stale) | s.f Moved (P7b/P7c prove tracked) | ACCEPT → runtime Invalid read | **FOLD-F67** (deref-site `checkSFieldUse` suffix-name gap; nullability NOT load-bearing) |
| `safe_free(p)`, `if(p){return}`, re-`safe_free(p)` | Nullable | Moved→Null (resurrected by MaybeSetNull on false edge) | ACCEPT | SOUND — at runtime `if(p)` is TRUE (post-free var still holds non-null bits) → re-free unreached; false branch only when p was originally null → re-free of null = no-op. vg 0 errors. |
| `_Nonnull owned`, `if(p){}` else `*p` (dead false branch) | NonNull (skips) | MaybeSetNull→Null | ACCEPT | SOUND — false branch of `if(_Nonnull)` is genuinely unreachable for a well-typed caller; no _Safe way to inject a null _Nonnull (nullptr/nullable→nonnull both rejected) |

**Conclusion (NO new root cause):** the nullability×ownership composition is JOINTLY SOUND on the
move/nullptr/field surface at 28656aa9. The two structural smells found —
(a) condition-use-of-moved bypasses `checkOPSUse`, and
(b) `MaybeSetNull` resurrects Moved→Null + `checkOPSUse:927` ignores Null for plain owned pointers —
do NOT compose into an exploit because (a) only reads non-null dangling BITS for a comparison (no
deref reaches), and (b) is masked by the runtime invariant that a just-freed variable still holds
non-null bits, so the only path reaching the masked re-free is the genuinely-null path (no-op free).
Every real deref/re-consume is independently caught by ownership's Moved state, which nullability's
staleness cannot defeat. The ONE accept-that-leaks (whole-struct-move then `*s.f`) is **FOLD-F67**
(pure ownership deref-site gap; nullability's stale NonNull is not the cause). Distinct from
F18/F48/F50 (those are intra-nullability compound-assign launders) and F75 (ownership merge union).
Recommend: composition surface SATURATED @ 28656aa9 for move/nullptr/field. Reopen-if `MaybeSetNull`,
`checkOPSUse:927`, or nullability gains a move/clear hook.

## getExprPathNullability / ShouldReportNullPtrError / IsStmtInSafeZone (BSCNullabilityCheck.cpp:312 / 464 / 430) — read + cast-launder PROBED 2026-06-04

**Invariant**: getExprPathNullability(E) must return Nullable whenever E may be null at the deref site;
every dereferencing Visit* (UnaryOperator/ArraySubscript/Member/CStyleCast/Call-arg/Return) reports iff
result==Nullable && ShouldReportNullPtrError. The latter = (NC_ALL) || IsStmtInSafeZone(S).

**Peers**: all deref Visit* share the (resolver, gate) pair — a hole in either has full blast radius.
getDefNullability (static type nullability), CurrStatusVD/CurrStatusFP/CurrStatusDPVD (path-sensitive
state; F26/F84/F85/F87/F89 live here), the cast-legality gates in Sema (which block laundering casts).

**Resolver dispatch** (canonical pointer type only): Paren/Safe/ImplicitCast recurse (transparent);
CStyleCast → getDefNullability(typeAsWritten); Call → arg0 for move/take builtins else
getDefNullability(retType); Conditional → Nullable if either branch nullable, NonNull if both, else
Unspecified; UnaryOp Deref → CurrStatusDPVD else getDefNullability; UnaryOp Addr* → NonNull; BinaryOp →
RHS only for Comma/Assign (else Unspecified); DeclRef/Member → getDefNullability then CurrStatus{VD,FP}.

**Candidates**:
1. CStyleCast nullability launder (`*(int *_Nonnull)(*p)` / `*(int *)(*p)`) — **PROBED SHAPE-REJECTED/SOUND**:
   nullable→nonnull cast rejected ("cannot cast nullable pointer to nonnull type"); nullable→raw stays
   Nullable (raw default = Nullable) so deref still flagged. Resolver's cast case is unreachable-with-launder.
2. BinaryOperator non-Comma/Assign → Unspecified (:381): `*(p + 0)`-style pointer-arith deref launders to
   Unspecified (not Nullable → no report). **PROBED-folded-into-F18** (mis-filed as F92, retracted) — `*(q+0)`/`*(q+1-1)` ACCEPTED while
   `*q`/`q[0]` rejected; runtime SIGSEGV (add(nullptr), valgrind Invalid read). C2 opcode-switch hole.
3. ConditionalOperator mixed (one NonNull, one Unspecified) → Unspecified (:351 break) — `*(c?nn:unspec)`
   not flagged. UNPROBED; reachability of an Unspecified branch in safe-zone deref unclear.

## return-type nullability contract enforcement — probing
**Invariant**: returning a `_Nullable` value from a `_Nonnull`-returning function must
be REJECTED (or require a narrowing on the path), else callers deref a maybe-null
assuming non-null (soundness FN).
**Peers**: CheckInit/FindNonnull (F78), assignment-nullability (F48/F66), getBSCDefNullability.
**Candidates**:
1. **`return p` (p _Nullable) from a `_Nonnull`-returning fn → FN if accepted — probing**.
2. return a narrowed-in-one-branch nullable (path-sensitive). UNPROBED.
3. return-via-wrapper (comma/paren) of nullable from _Nonnull fn. UNPROBED.

## static-local _Nonnull scalar zero-init (F78 sibling, soundness direction) — probing
**Invariant**: a `static int *_Nonnull p;` (zero-init = null) violates _Nonnull and
must be REJECTED; the properly-initialized form must be ACCEPTED. F78 was the union
FP (over-reject); this is the scalar soundness direction.
**Peers**: F78 (static union nullability FP), CheckInit/CheckGlobalInit.
**Candidates**:
1. **null-init static _Nonnull scalar → REJECT (sound)? — probing**.
2. properly-init static _Nonnull → ACCEPT (control). 
3. static _Nonnull set null then deref. UNPROBED.

## nullptr-to-_Nonnull assignment — probing
**Invariant**: assigning `nullptr` (or a _Nullable value) to a `_Nonnull` pointer
must be REJECTED (or narrowing-required), else the _Nonnull contract is broken.
**Peers**: return-nullability (cycle 17, sound), CheckInit, assignment-nullability F66.
**Candidates**:
1. **`p = nullptr` where p is _Nonnull → REJECT? — probing**.
2. `p = nullableValue` to _Nonnull. UNPROBED.
3. wrapper-wrapped nullptr assign `p = (0, nullptr)`. UNPROBED.

## nullability of function-call result — probing
**Invariant**: deref of a `_Nonnull`-returning call result is OK (no check); deref
of a `_Nullable`-returning call result must be REJECTED (needs a null-check first).
**Peers**: return-nullability (cycle 17), getExprPathNullability call-site (F18/F87).
**Candidates**:
1. **deref `_Nullable`-returning call result with no check → REJECT? — probing**.
2. deref `_Nonnull`-returning call result → ACCEPT (control).

## narrowing survives unrelated call (precision) — probing
**Invariant**: narrowing of p survives a call that does NOT take p (precision);
only a call that could mutate p drops it (F87 = soundness direction, filed).
**Candidates**: 1. `if(p){ noop(); return *p; }` → *p accepted (narrow survives)?

## narrowing-invalidation through _Unsafe block — probing
**Invariant**: a mutation of p inside `_Unsafe { p = nullptr; }` must invalidate p's
narrowing (the analysis tracks through _Unsafe blocks, like ownership cycle-23);
else narrowing wrongly survives → null deref FN.
**Peers**: F87/F89 (call/addr-taken mutate), narrowing-invalidation-fuzz oracle, cycle-23 (_Unsafe ownership).
**Candidates**: 1. `if(p){ _Unsafe{p=nullptr;} *p; }` → REJECT (invalidated)?

## narrowing survives call that mutates a GLOBAL (not an arg) — F87-distinct? — probing
**Invariant**: a call that nulls a narrowed global's field via the global directly
(global NOT passed as an arg) must invalidate that field's narrowing. F87's fix
clears narrowing keyed on _Borrow ARGS only → a global mutation would be MISSED (FN,
distinct fix surface from F87).
**Candidates**: 1. `if(gs.f){ *gs.f; clear_global(); *gs.f; }` (clear_global nulls gs.f) → 2nd deref rejected?

## mergeVD one-sided-narrowing at join (F26 sibling, DISTINCT function) — probing
**Invariant**: at a join, a var narrowed NonNull on ONE predecessor but Nullable
(absent) on the other must MEET to Nullable. mergeVD:844 `else { statusA[VD]=NK; }`
COPIES the present-in-one NonNull instead of meeting → FN (same bug as F26/mergeDPVD,
different function/map). mergeFP:860 identical.
**Candidates**:
1. mergeVD — SHAPE-SOUND: _Nullable vars/params are PRE-TRACKED (Nullable at entry), never absent → merge meets correctly. No FN.
2. **mergeFP — FieldPaths are NOT pre-tracked (added on narrowing); a field narrowed on one join branch is ABSENT on the other → mergeFP copies NonNull → FN? — probing** (the real F26 sibling).

## mergeFP for DEEPER field path (non-pre-populated?) at join — probing
**Invariant**: F26 says single-level FieldDecls are pre-populated Nullable (masking
the mergeFP copy-bug). DEEPER paths `{s,"->f->g"}` may NOT be pre-populated → a deeper
field narrowed on one join branch is ABSENT on the other → mergeFP copies NonNull → FN.
**Candidates**: 1. `if(cond){ if(!s->f->g) return; } *s->f->g;` → join FN if deeper path unpopulated?

## narrowing propagation through switch (>2-successor terminator, :790) — probing
**Invariant**: a narrowing established BEFORE a switch must survive INTO the switch
cases. :790 "only handle terminators with two successors" — a switch (>2 succ) may
drop/not-propagate the incoming narrowing → FP (over-reject inside cases).
**Candidates**: 1. `if(p){ switch(x){ case 1: return *p; } }` → *p accepted (narrow survives) or FP?

## VisitArraySubscriptExpr (:687) — read, SOUND
Checks `getExprPathNullability(ASE->getBase())==Nullable` → diagnoses NullablePointerDereference.
For `p[i]` base is `p` → nullable p caught; `(p+off)[i]` base is arithmetic → folds into F18
(getExprPathNullability BO_Add/Sub→Unspecified hole). Consistent with deref/member paths. No new gap.

## mergeVD/mergeFP/mergeDPVD (:844/:860/:876) — read + probed, SOUND (C5 home)
INVARIANT: join merges statusB into statusA with "Nullable wins" — for a key in both,
result = (B is Nullable ? Nullable : A's value); for a key only in B, take B's value; keys only
in A are kept. Sound direction (a value nullable on ANY incoming path is nullable after the join).
ASYMMETRY CHECKED (the loop only iterates statusB → keys-in-A-not-B kept unchanged): does NOT
create an FN, because live VDs are tracked on ALL paths (params seeded at entry, locals at DeclStmt),
so at any join a live VD is in BOTH maps → "Nullable wins" applies. Behavioral confirmation: p
NonNull on one branch + Nullable on the other → merge Nullable → deref REJECTED (param AND local).
The keys-only-in-one-map case is block-scoped vars not live on the other path (taking the present
value is correct). PEERS: runOnBlock (:894), VisitDeclStmt/CheckInit (:472/:483, count-gated update).
No C5 merge hole found.

## VisitBinaryOperator (:588) — read, assignment-narrowing core (known holes filed)
INVARIANT: for `LHS = RHS` (pointer LHS): RHSKind=getExprPathNullability(RHS) [F18 arithmetic
hole lives here], LHSKind=getDefNullability. Three LHS shapes:
- deref-path `*p=` (HasLHSDP, depth>0): NonNull←Nullable diag; else update CurrStatusDPVD; then
  InvalidateDeeperDerefStatusForPath [F85/F86 deeper-stale area — F86 filed].
- var `p=` : NonNull←Nullable diag; else (count-gated) CurrStatusVD[VD]=RHSKind; InvalidateDerefStatusForVar (rebind stales deref facts).
- member `s.f=` : NonNull-member←Nullable diag; else (count-gated) CurrStatusFP[FP]=RHSKind [F84/F85 field area].
count-gating = optimization over seeded map (params@entry, locals@declstmt — both tracked, per mergeVD probes).
KNOWN HOLES all filed: F18 (RHS BO_Add/Sub→Unspecified), F84/F85 (field stale-narrowing), F86 (deeper-stale 3-site). No NEW gap.
NULLABILITY ANALYSIS now comprehensively read: Visit family (8) + merge (3) + assignment-narrowing core.

## getExprPathNullability — CStyleCast arm (BSCNullabilityCheck.cpp:353-355) — PROBED-SOUND (cast-launder hunt 2026-06-23)

**Verdict (2026-06-23)**: cast-launder SOUND across direct/return/field/nested/reborrow/var-assign/typedef routes. The :354 arm IS a value-state launder (returns destType nullability, discards subexpr path-state), BUT it is backstopped at every consumer: VisitCStyleCastExpr :708 self-check fires on the cast, plus SEMA-LEVEL diags (DiagnosticBSCSemaKinds.td:366/368/372) fire identically for cast and uncast wherever the analyzer runs (_Safe or -nullability-check=all). Non-_Safe default acceptance is the standard SZ gate (== uncast; folds with F86 caveat), not a launder. bug_log.md:1412 already notes this site is known. See _probed.md 2026-06-23.

**Invariant**: the path-nullability of a cast expression should reflect whether the
*underlying value* can be null, NOT merely the cast's written destination type.
Currently line 353-355 returns `getDefNullability(getTypeAsWritten())` — it trusts
the cast annotation and discards the subexpr's actual path-state.

**Peers**: VisitCStyleCastExpr (:708, the direct cast→nonnull check), VisitUnaryOperator
deref (:677, consumes getExprPathNullability(subexpr)), VisitReturnStmt (:721),
VisitCallExpr arg (:660). getDefNullability (:239: raw ptr = Nullable; owned/borrow
no-annotation = NonNull). NormalizeInitExpr (:285, strips CStyleCast for INIT path —
note this is a DIFFERENT consumer that DOES strip casts).

**Candidates**:
1. `*( (T*_Nonnull)nullable_p )` — deref of a nonnull-cast of nullable. getExprPathNullability
   on the cast returns NonNull (line 354) → VisitUnaryOperator deref ACCEPTS. BUT
   VisitCStyleCastExpr (:708) ALSO fires on the same cast (subexpr is directly nullable_p).
   Net: 1 diag (the cast) so NOT silent-FN unless the cast diag is itself suppressible.
   Check: does the cast diag fire outside _Safe / only under -nullability-check?
2. Round-trip `(T*_Nonnull)(void*)nullable_p` — outer cast dest is NonNull; its SUBEXPR
   is `(void*)nullable_p` (a CStyleCast). VisitCStyleCastExpr on the OUTER cast asks
   getExprPathNullability(subexpr=inner void* cast) → line 354 returns getDefNullability(void*)
   = Nullable (raw) → so OUTER cast IS caught. Inner cast dest void* not NonNull → inner not
   flagged. Net caught. Probe to confirm.
3. Cast result ASSIGNED to a _Nonnull LHS then deref'd separately: `int*_Nonnull q=(int*_Nonnull)p; *q;`
   — the cast-assign is caught by VisitCStyleCastExpr; if q then carries NonNull state the deref
   is clean. Tests whether the LHS assign is the only gate.
4. Cast inside `&_Mut *` reborrow then deref — F13-adjacent.

## Generic-body deref of a `_Nullable` type-argument (monomorphization × VisitUnaryOperator/VisitArraySubscriptExpr) — PROBED-folded-into-G12 (with G12 severity-upgrade signal)

**OUTCOME (2026-06-23)**: FOLDED into G12 by root cause (substitution strips nullability sugar from the type-arg), BUT carries a **containment-break that contradicts G12's MEDIUM rationale** — recommend G12 MEDIUM→HIGH. Detail:
- ast-dump confirms the instantiated generic-struct field `v` has type `int *_Borrow` — the `_Nullable` AttributedType sugar is STRIPPED during template-arg substitution (param `b` still PRINTS as `Box<int*_Borrow _Nullable>` but its canonical/field form is `Box<int*_Borrow>`). Same mechanism G12 documents for fn params/returns.
- The deref `*b.v` of a `Box<int*_Nullable>` field is ACCEPTED (FN); non-generic `BoxNN{int*_Nullable v;}` `*b.v` is correctly REJECTED ("nullable pointer cannot be dereferenced").
- read-into-_Nonnull (`int*_Nonnull r=b.v`), pass-to-_Nonnull-param (`takenn(b.v)`), and direct deref `*b.v` are ALL accepted → field is laundered to default-NonNull raw `int*_Borrow`.
- **CONTAINMENT BROKEN**: G12's note says "any downstream deref of the laundered value is still independently nullability-checked" (the basis for MEDIUM). FALSE for the struct-field case: the launder is STRUCTURAL (baked into the instantiated field's declared type), so a deref OUTSIDE any generic fn — `int r = *b.v;` directly in `main` — is ALSO accepted. RUNTIME: `b.v = nullptr; *b.v` compiles clean, valgrind = "Invalid read of size 4" (real null-deref reachable in-BSC). G12's exemplar laundered at a CALL boundary (the original var retained its sugar for later checks); the struct-field launder is permanent → runtime-reachable null-deref → HIGH, not MEDIUM.
- Repro: /tmp/explorer_rt_box_null.cbs (clean compile + valgrind Invalid read). Baseline: /tmp/explorer_baseline_box_null.cbs (rejected). Containment-break: /tmp/explorer_outside_deref.cbs (clean even outside generic fn).

### original note (pre-probe):

**Invariant**: after a generic fn/struct is instantiated with `T = U *_Nullable`, every `*x` / `x[i]` / `x->m` inside the (monomorphized) body where `x` has type `T` must still be nullability-checked exactly as if the body were hand-written with `U *_Nullable x`.
**Peers**: G12 (call-site *accept* direction: `_Nonnull` type-arg drops, nullable arg accepted), G01 (conditional-type-alias desugar drop), getDefNullability (:658), getExprPathNullability (:307), VisitUnaryOperator (:669 UO_Deref).
**Mechanism hypothesis**: G12 established that template-arg substitution canonicalizes the type-arg, stripping the nullability AttributedType sugar. If the substituted `T x` param in the monomorphized body has a *canonical raw pointer* type (sugar gone), then `getExprPathNullability(x)`/`getDefNullability` see Unspecified, NOT Nullable → the `*x` deref check (VisitUnaryOperator :669) never fires → FN: an unchecked nullable deref inside a monomorphized generic body. This is the *deref-inside-body* mirror of G12's *accept-at-call* — same root (sugar stripped during subst) but a SEPARATE observable (a deref the body performs, vs an arg the call passes). Could be HIGH if a runtime null-deref is reachable (the deref is INSIDE the body, no separate downstream check to backstop it — unlike G12 where the laundered value's later deref is independently checked).
**Candidates**:
1. `T deref<T>(T x){ return *x; }` then `deref<int *_Nullable>(nullptr)` — is the `*x` deref flagged? Non-generic `int deref_nn(int *_Nullable x){return *x;}` IS flagged. (reachability + asymmetry)
2. generic struct `Box<T>{T v;}` with `Box<int*_Nullable> b; ... *b.v;` — member-path deref of a nullable type-arg field. (composition)
3. `T x` param, body `if (x) *x; else *x;` — narrowing CFG over a canonicalized-raw T (does narrowing even have a Nullable base to narrow from?). (reachability)

## VisitArraySubscriptExpr / VisitMemberExpr (BSCNullabilityCheck.cpp:687-704) — read 2026-06-24
**Invariant**: `arr[i]` flags iff the BASE is Nullable (subscripting a nullable pointer = deref);
`p->f` flags iff base p is Nullable. Both delegate to getExprPathNullability(base).
**Peers**: VisitUnaryOperator (deref, F18), getExprPathNullability (ArraySubscript-static branch),
the "pointers from array access are untrackable" spec rule (3-nonnull:69).
**Candidates**: 1. **deref of a `_Nullable` ELEMENT of an array `*arr[i]`** — if getExprPathNullability(arr[i])
returns Unspecified (untrackable array access) not Nullable, the UnaryDeref check never fires → silent
null-deref FN. UNPROBED ⭐. 2. `arr[i]->f` member-access on nullable element. 3. nested `arr[i][j]`.

## getDefNullability / FindNonnull (BSCNullabilityCheck.cpp:239/256) — read 2026-06-25 (shared nullability gate)
INVARIANT: map a QualType to its NullabilityKind — explicit _Nonnull/_Nullable from QT->getNullability(sugared)
wins; else owned/borrow-qualified → NonNull (unless explicit Nullable); else raw pointer → Nullable default;
non-pointer → Unspecified. FindNonnull is the "type contains nonnull" variant (recurses arrays = F78 area).
CANDIDATES (no new): the gate is CORRECT — it reads sugared nullability properly + sound defaults. The G12 FN
(generic template-subst STRIPS the _Nonnull sugar → getNullability returns None → raw pointer → Nullable default
→ nonnull lost) and F28 FN (indirect call never reaches the gate: getDirectCallee null) are UPSTREAM of this gate,
not defects in it. getDefNullability/FindNonnull SOUND; the nullability defects live in the sugar-preservation /
gate-invocation paths (G01/G12/F28/F31), not the gate logic.

## VisitMEForFieldPath (BSCNullabilityCheck.cpp:149) — field-path key builder (adjacent-fix re-probe of ced6364, 2026-06-26)
INVARIANT: builds FieldPath (root VarDecl* `first` + ".f"/"*"-chain `second`) uniquely identifying a member lvalue,
so narrowing facts (StatusFP) for distinct objects/paths never collide. Peels MemberExpr/DeclRefExpr/ImplicitCast/
ParenExpr/UO_Deref (:150-166); ced6364 added the UO_Deref "*" arm.
PEERS: getVarDeclFromExpr (:170) + getMemberExprFromExpr (:186) — BOTH also peel SafeExpr (:178/:193); mergeFP/StatusFP.
CANDIDATES:
1. (C6 SafeExpr-not-peeled, UNPROBED top) VisitMEForFieldPath does NOT peel SafeExpr (asymmetry vs the two sibling
   helpers). A SafeExpr-wrapped base → none of the arms match → FP.first stays null → `a.f` and `b.f` could both key
   (null, ".f") → narrowing a.f leaks to b.f (FN: b.f wrongly NonNull). Probe: if(a.f) sink_nonnull(b.f).
2. (ArraySubscriptExpr base, UNPROBED) `arr[i].f` base hits no arm → null root → same collision class.
3. (CStyleCastExpr base, UNPROBED) only ImplicitCast peeled; explicit-cast base stops recursion → null root.

## VisitArraySubscriptExpr / VisitMemberExpr-arrow (BSCNullabilityCheck.cpp:691/700) — deref-via-subscript/arrow (2026-06-26 Mode-1)
INVARIANT: `p[i]` (=*(p+i)) and `p->a` deref the base, so if getExprPathNullability(base)==Nullable && ShouldReport
NullPtrError → emit NullablePointerDereference/AccessMember. PEERS: VisitUnaryOperator UO_Deref (same pattern),
getExprPathNullability (path-nullability, narrowing-aware), ShouldReportNullPtrError (zone/opt-in gate).
CANDIDATES: 1. ShouldReportNullPtrError gate suppression (FN if wrongly false) — UNPROBED but the indirect-call gaps
are F28/F31 (filed); direct subscript/arrow are checked. 2. base path-nullability mis-class (narrowing PROBED-SOUND).
3. `p.a` dot correctly NOT checked (no deref). Looks sound; the deref-via-subscript/arrow nullability is covered.

## getExprPathNullability (BSCNullabilityCheck.cpp:317) — core path-sensitive nullability lookup (2026-06-26 Mode-1)
INVARIANT: returns the path-nullability of E: null-expr→Nullable, StringLiteral→NonNull; for pointer types peels
ParenExpr/SafeExpr/ImplicitCast (:326-330), raw-transfer builtins (__move_to_raw etc.) recurse the arg (:333-339),
CallExpr→getDefNullability(return type) (:343), ConditionalOperator→merge arms. PEERS: getDefNullability (declared
default), all Visit* deref checks consume this, narrowing (path-facts override). 
CANDIDATES: 1. (CStyleCastExpr peel) explicit cast not in the switch → falls to getDefNullability(cast-type); a
nullable value cast loses Nullable path-nullability → FN — BUT cast-to-nonnull is rejected at the cast site (backstop,
PROBED-SOUND). UNPROBED for cast-to-raw. 2. CallExpr uses DECLARED return nullability (F31 callee-gap filed). 3. default
fall-through → getDefNullability (conservative declared default). Core lookup peels the common wrappers; looks sound.

## getDefNullability / FindNonnull (BSCNullabilityCheck.cpp:244/261) — declared-nullability lookup (2026-06-26 Mode-1)
INVARIANT: type's declared nullability — explicit _Nonnull/_Nullable honored (:247); Owned/Borrow → NonNull (implicitly
valid, :251-254) unless explicitly Nullable; RAW pointer → Nullable (conservative default, :256); non-pointer →
Unspecified. PEERS: getExprPathNullability (consumes this), the deref checks. CANDIDATES: 1. owned→NonNull trust = basis
of the composition_init_null cross-analyzer gap (nullability trusts owned=NonNull w/o verifying init; documented, opt-in
-nullability-check=all). 2. raw→Nullable is conservative-safe (over-checks = FP not FN). 3. non-pointer Unspecified sound.
Conservative defaults, sound.

## VisitCallExpr (:659) + deref checks (BSCNullabilityCheck.cpp, 2026-06-27)
VisitCallExpr: for CE->getDirectCallee() (DIRECT calls only), checks each NonNull param vs a Nullable arg → PassNullableArgument.
Deref family: VisitUnaryOperator UO_Deref `*p` (679), VisitArraySubscriptExpr `p[i]` (692), VisitMemberExpr arrow `p->a` (701)
— all flag NullablePointerDereference when base Nullable; &mut*p/&const*p allowed (no real deref). CSCE cast-to-nonnull (713).
CANDIDATES: 1. (F31, filed) indirect call `fp()` — getDirectCallee()==null → callee fnptr nullability never checked → call a
null fp = SIGSEGV. 2. (SAME root extension) args to an INDIRECT call's NonNull params also unchecked (loop skipped when
getDirectCallee null) → fp(nullable_arg) unflagged. PROBED-sound: indirect-call ARGS caught by assignment-compat check (exit=1); F31 bounded to CALLEE deref only. (one fix: VisitCallExpr
handle indirect calls covers callee + args). 3. deref family comprehensive (deref/subscript/arrow all covered), sound.

## VisitBinaryOperator (:593) — assignment nullability + flow-sensitive tracking (2026-06-27)
INVARIANT: on pointer-assignment, if LHS def-nullability NonNull && RHS path-nullability Nullable → NonnullAssignedByNullable
(F18 mechanism); else update CurrStatusVD[VD]/CurrStatusDPVD[derefpath]=RHSKind (narrowing). REBIND invalidates stale facts:
InvalidateDerefStatusForVar(VD) on var-rebind, InvalidateDeeperDerefStatusForPath on deref-path write. Handles VarDecl,
deref-path (*p), MemberExpr(field). PEERS: VisitUnaryOperator deref-check, getExprPathNullability (consumes CurrStatus).
CANDIDATES: 1. (rebind-invalidation) narrow p nonnull via guard, rebind p=nullable, then *p — is the stale nonnull fact
invalidated (→*p flagged)? UNPROBED → probing. 2. deref-path CurrStatusDPVD staleness (F92 area). 3. member-field assign sound.

## VisitCStyleCastExpr (:713) + VisitReturnStmt (:726) — cast/return nullability (2026-06-27)
INVARIANT: VisitCStyleCastExpr — if cast TARGET (getTypeAsWritten) is NonNull and subexpr (IgnoreParenImpCasts) path-nullability
is Nullable → NullableCastNonnull diag. Covers (int*_Nonnull)p, (int*borrow)p, (int*owned)p. VisitReturnStmt — NonNull return
type + Nullable retval → ReturnNullable. PEERS: VisitBinaryOperator(assign), getExprPathNullability. CANDIDATES: 1. double
explicit cast `(nonnull)(borrow)nullable` — IgnoreParenImpCasts doesn't peel the inner EXPLICIT cast; does the inner cast's
path-nullability launder it? UNPROBED→probing. 2. getTypeAsWritten typedef-nonnull resolved (sound). 3. return-nullable covered.

## CheckInit (BSCNullabilityCheck.cpp:488) — decl-time nullability init (2026-06-27)
INVARIANT: no-init (or ImplicitValueInit) + Nonnull type → NonnullInitByDefault (F81 area, fixed); pointer init with Nonnull
LHS + Nullable RHS → NonnullAssignedByNullable; else update CurrStatusVD/CurrStatusFP narrowing; decl-time rebinding stales
deref-chain facts (InvalidateDerefStatusForVar). PEERS: VisitDeclStmt(:477), VisitBinaryOperator(assign). CANDIDATES: 1.
nonnull-default-init=F81(fixed). 2. nonnull←nullable-init covered. 3. narrowing-at-init sound. All sound. [Probing a separate
init×borrow interaction: borrow of an UNINITIALIZED owned.]

## getExprPathNullability BinaryOperator case (:381) — F92 ROOT (2026-06-27, precise)
The switch case Expr::BinaryOperatorClass (:381) handles ONLY BO_Comma/BO_Assign (recurse RHS); BO_Add/BO_Sub (pointer
arithmetic) hit `break` → fallback → path-nullability not computed nullable. So `*(q+n)`/`*(q-n)` for a _Nullable q launder
(deref not flagged) — F92. Confirmed: `*(q-1)`(BO_Sub) + `*(q+0)`(BO_Add) both unflagged; only `*q`(control) caught.
FIX = add BO_Add/BO_Sub to :381 (recurse the pointer operand). (F18 = the ASSIGN manifestation, caught via CheckNullability
QualTypeAssignment; F92 = the DEREF manifestation via this getExprPathNullability gap — still open.) Full switch otherwise
sound: Paren/Safe/ImplicitCast/Call(raw-transfer)/Conditional/CStyleCast/UnaryOp/InitList/DeclRef/ArraySubscript all handled.

## NullabilityCheckImpl::mergeVD (BSCNullabilityCheck.cpp:849) — CFG-join meet — PROBED-SOUND 2026-06-29
**Invariant**: at a CFG join, a VD's merged nullability is Nullable if Nullable on ANY predecessor (meet — nullable
wins): `statusA[VD] = NK==Nullable ? Nullable : statusA[VD]`; a VD present in only one pred is added with that pred's
NK. Soundness relies on `initStatus` (:818) pre-seeding EVERY nullable VD as Nullable at entry, so the "unnarrowed"
path always carries p=Nullable into the merge (no absence→over-narrow). **Peers**: mergeFP (FieldPath merge, F84 area),
SetCFGBlocksByExpr (:806, the F122 then/else narrowing), initStatus.
**Probe** asymmetric narrowing then join: `if(c){ if(!p) return; need(p); } need(p);` and `if(c){ if(p){need(p);} } need(p);`
→ both REJECT the post-join `need(p)` (p Nullable on the c-false path); control `if(!p) return; need(p);` ACCEPTS.
The C5 merge-state UNDER-approximation that causes F75 in OWNERSHIP (merge unions the owned-field set) does NOT exist
here — mergeVD is a proper meet. **Candidates**: 1. (C5 merge under-approx) REJECTED — proper meet, sound. 2. mergeFP
field-path merge (F84-adjacent) — covered. 3. goto/switch irreducible-join meet — narrowing note Chain X SOUND.

## getDefNullability (BSCNullabilityCheck.cpp) — declared-nullability classifier — PROBED-SOUND 2026-06-29
**Invariant**: returns the declared nullability of a pointer type for tracking decisions. Explicit `_Nonnull`/`_Nullable`
(via `QT->getNullability(Ctx)`, sugar-aware) honored; Owned/Borrow (`CanQT.isOwnedQualified/isBorrowQualified`) default
NonNull (Nullable only if explicit); raw pointers default Nullable; non-pointer → Unspecified. Used by `initStatus`
(:818) to seed which VDs/FDs get null-tracked (only Nullable ones).
**Peers**: FindNonnull (sibling, handles array element types too), initStatus, getExprPathNullability.
**Probe** typedef sugar: `typedef int *_Nullable NP; f(NP p){ need(p); }` → REJECT; `typedef int *RP` (raw→nullable)
→ REJECT; double-nest `typedef NP NP2` → REJECT. Sugar/typedef-hidden nullability is correctly seen → tracked.
No FN from typedef-laundered nullability. **Candidates**: 1. (typedef sugar escape) REJECTED — sugar-aware, sound.
2. pointer-to-pointer outer-tracking (cf F64 ownership analog) — standard outer-pointer tracking, UNPROBED low-pri.
3. FindNonnull array-element path — UNPROBED, adjacent.

## VisitCallExpr arg-nullability (BSCNullabilityCheck.cpp:659) — read 2026-06-29
**Invariant**: for a DIRECT callee, each _Nonnull param whose arg has path-nullability Nullable → PassNullableArgument error. Gated by getDefNullability(param) + getExprPathNullability(arg) + ShouldReportNullPtrError. **Peers**: getExprPathNullability (F92 gap inherited by args), getDefNullability, ShouldReportNullPtrError. **Candidates**:
1. (indirect-call arg, FILED) `getDirectCallee()` skips fnptr callees → F28/F31.
2. (arg-launder via getExprPathNullability) a nullable arg whose path-nullability is mis-classified → folds into F92 (BO_Add/BO_Sub) — same getExprPathNullability single-root.
3. variadic args beyond getNumParams — OOS (variadic-in-_Safe).

## getExprPathNullability / path-builder ArraySubscriptExpr coverage (BSCNullabilityCheck.cpp:150-180, 317)
- **Invariant**: every l-value expression that can carry a narrowable nullability fact should get a stable path key so `!=nullptr` guards narrow it.
- **Observation (2026-06-29)**: path-builder handles MemberExpr/DeclRefExpr/ICE/Paren/UnaryOperator; NO ArraySubscriptExpr case → `arr[i]` nullability is not narrowed. LATENT-UNREACHABLE in-scope (see _probed 2026-06-29): _Borrow-array/subscript restrictions + F97 array-init granularity + param-decay block a clean FP. Candidate (UNPROBED, shape-blocked): find a reachable _Nullable l-value that routes through ArraySubscriptExpr without _Borrow/array/F97 restrictions.

## mergeVD / mergeFP (BSCNullabilityCheck.cpp:849/865) — nullability-state CFG join
- **Invariant**: at a CFG join, a VD/FieldPath is NonNull only if NonNull on ALL predecessor paths; Nullable if Nullable on ANY (sound meet for deref-safety).
- **Peers**: runOnBlock (:1217 calls mergeVD over predecessors), VisitBinaryOperator (narrowing), getExprPathNullability.
- **Structure**: iterate statusB; for VD in both, `result = (B==Nullable) ? Nullable : A` (Nullable wins); for VD only in statusB, add it; **VD only in statusA (absent from B) is left UNCHANGED**.
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: the "VD absent from B keeps statusA" branch is SAFE because status maps are DENSE — every var is present at its declared nullability on every path. Test: `if(c){p=nn;} sink(p)` (one-branch assignment-narrow) REJECTED post-join "cannot pass nullable pointer argument" (merge gives Nullable correctly); `if(p!=nullptr){sink(p);} sink(p)` post-if also Nullable; both-branch narrow → clean rc=0. A one-branch NonNull narrow never survives the merge — the untouched branch carries p=Nullable, so meet = Nullable. No stale-narrow FN. (2) mergeFP (:865) identical structure for FieldPaths — same question. (3) the Nullable-wins direction (both-present) is sound.

## TransferFunctions::VisitReturnStmt (BSCNullabilityCheck.cpp:726) — return-nonnull verification
- **Invariant**: a `_Nonnull`-returning function must not `return` a flow-sensitively-Nullable value (else the caller derefs null).
- **Peers**: getExprPathNullability (RV nullability), ShouldReportNullPtrError (suppression gate, F-area), getDefNullability.
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: `return p` (Nullable) from `_Nonnull` → "cannot return nullable pointer type"; `if(p==null)return fb; return p` (narrowed) → rc=0 accepted. Core return-nonnull check catches nullable returns + respects flow-sensitive narrowing. (2) the check uses top-level `getDefNullability(ReturnType)` only — a NESTED nonnull (returned struct's `_Nonnull` field uninit/null) is not checked here (init-analysis's job). (3) ShouldReportNullPtrError suppression — could it hide a real return-nullable?

## getDerefPathVDFromExpr (BSCNullabilityCheck.cpp:201) — variable deref-chain path for nullability
- **Invariant**: builds (VD, depth) for `p`/`*p`/`**p` so deref-chain nullability facts can be narrowed/invalidated; handles DeclRefExpr + UnaryDeref only (fields use the separate FieldPath/getExprPathNullability mechanism).
- **Peers**: getExprPathNullability (field/member), SetCFGBlocksByExpr, InvalidateDerefStatusForVar, mergeDPVD.
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: `if(*pp!=nullptr){ need(*pp) }` → rc=0 (the (pp,1) deref-chain narrows to nonnull inside the guard); unguarded `need(*pp)` → rc=1 (nullable caught). Deref-chain narrowing sound. (2) MemberExpr not handled → `*(s.f)` deref-of-field has no var-deref-path (separate FieldPath; check no gap). (3) IgnoreParenImpCastsSafe — a cast in the deref chain breaking path identity.

## ShouldReportNullPtrError + CheckInit (BSCNullabilityCheck.cpp:469/482) — safe-zone gating + default-init nonnull
- **Invariant**: null-ptr errors reported when zone=NC_ALL or the stmt is in a _Safe zone; a `_Nonnull` field/var default-zero-initialized (no init / ImplicitValueInit) → NonnullInitByDefault diag.
- **Peers**: IsStmtInSafeZone, FindNonnull, VisitDeclStmt (static-local), VisitReturnStmt.
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: `struct S{int*_Nonnull f;}; struct S s={0};` → rc=1 "nonnull pointer cannot be assigned by nullable pointer" (default-zero _Nonnull caught). (2) **PROBED-SOUND**: deref-nullable `*p` in an _Unsafe block within _Safe → rc=0 suppressed (correct — _Unsafe opts out of the nullability check, user takes responsibility). (3) static-local _Nonnull no-init (default-zero) reported.

## getExprPathNullability BinaryOperator dispatch (BSCNullabilityCheck.cpp:~382) — F92 root
- **Invariant**: resolve the flow-sensitive nullability of a pointer expression through its structure; a BinaryOperator recurses to the pointer operand.
- **Peers**: getVarDeclFromExpr, VisitReturnStmt, getExprPathNullability (self-recursive), F92 (filed).
- **Candidates (F92 CONFIRMED)**: (1) the BinaryOperator arm (:383) handles ONLY `BO_Comma`/`BO_Assign` (recurse RHS); `BO_Add`/`BO_Sub` (pointer arithmetic `q+n`) fall through to Unspecified → deref `*(q+n)` not null-checked = F92. (2) other opcodes (Mul/And/…) N/A for pointers (can't multiply/bitwise a pointer) → BO_Add/BO_Sub are the COMPLETE relevant gap. (3) fix = add BO_Add/BO_Sub recursing the pointer operand; one fix closes F92 (incl. commuted `n+q`, `q-n`).

## TransferFunctions::InvalidateDerefStatusForVar + VisitBinaryOperator assign (BSCNullabilityCheck.cpp:588, 593) — narrowing invalidation on reassign
- **Invariant**: reassigning a var/deref-path that was narrowed to NonNull (via `if(p)`) must invalidate the stale narrow so a later deref of the now-possibly-null value is re-checked.
- **Peers**: getExprPathNullability (F92), SetCFGBlocksByExpr narrowing (:754), CurrStatusVD (var narrows) vs CurrStatusDPVD (deref-path narrows).
- **Candidates**: (1) **PROBED-SOUND (direct)** + **CONFIRMED-F127 (escape)**: direct reassign `if(p){ p=q; *p }` → rc=1 (narrow invalidated, sound). BUT address-escape `if(p){ mutate(&p); *p }` (mutate nulls p via `*pp`) → rc=0 accepted → runtime SIGSEGV = **F127 (HIGH, FILED)**. Root: VisitCallExpr (:659) never invalidates a var's narrow when its address escapes to a writable-pointer param. Owned analyzer tracks escape (GetAddr); nullability doesn't. (2) deref-path stale: `if(*pp){ *pp = null; **pp; }`. (3) narrow-then-call-that-may-null: `if(p){ mutate(&p); *p; }` (address escape invalidation).
