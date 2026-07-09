# BSCOwnership.cpp

Source: `clang/lib/Analysis/BSC/BSCOwnership.cpp`.

Two top-level concerns: (1) the `OwnershipImpl::OwnershipStatus` bit-lattice + its operators, (2) the `TransferFunctions` visitor that drives CFG-based dataflow.

## TransferFunctions::VisitReturnStmt (BSCOwnership.cpp:2404-2422) — read 2026-06-17

**Invariant**: a `return E` moves ownership of E out to the caller (op=Move), unless the
function returns an integer (op=GetAddr — "integer-returning fns can't consume ownership").
Cast-from-void-ptr-with-owned-fields in a return is diagnosed (:2406).

**Candidate (composition × path-sensitivity)**: `return c ? p : q;` (both p,q `_Owned`,
owned return type) — per CFG path only the TAKEN arm is moved out; the OTHER owned value
is still live at the return → must LEAK on that path. If the ternary visits BOTH arms with
op=Move (marking both moved), the non-returned one's leak is MISSED (FN). If path-split
(only taken arm moved per path), the non-returned arm is correctly flagged leak. PROBE which.

**Peers**: VisitAbstractConditionalOperator (:2187, ternary move handling), checkMemoryLeak
(:2427 at LifetimeEnds — scope-end leak oracle), VisitCallExpr (arg-move op).

## TransferFunctions::VisitArraySubscriptExpr (BSCOwnership.cpp:2044-2066) — read 2026-06-17

**Invariant**: a subscript expr must propagate ownership effects of ALL sub-exprs (base AND index).

**GAP spotted**: processes ONLY the base — peels nested ASE building a "*" suffix, then
`VisitDeclRefExpr(base,suffix)` or `Visit(ASE->getBase())` (:2065). The INDEX is NEVER
visited. A side-effecting MOVE in index position (`arr[consume(p)]`, consume takes
`int *_Owned`, returns int) would be invisible → p's move not tracked → false-leak (FP)
OR accepted double-free (FN/unsound) if p freed again.

**Peers**: VisitBinaryOperator (visits BOTH operands — contrast); VisitCallExpr (op=Move
for args, only if reached); VisitStmt (default child-iter, overridden here).

**Candidates**: 1. (C3) index not visited → SHAPE-REJECTED (2026-06-17): CFG linearizes the expr so consume(p) is its own element visited by VisitCallExpr; as2 confirms double-free after `arr[consume(p)]` IS caught. AST visitor index-skip is moot — dataflow runs over CFG elements. (Side: element-wise-init `arr[i]` uninit = F97-RETRACTED/intended.)
2. nested `m[consume(p)][i]`. 3. symmetry vs VisitBinaryOperator.

## State model

Status bits (in `OPSStatus` / `SStatus` / `BOPStatus`):
`Uninitialized=0x1`, `Null=0x2`, `Owned=0x4`, `Moved=0x8`, `PartialMoved=0x10`, `AllMoved=0x20`.

Three parallel dictionaries keyed by `VarDecl*`:
- `OPSStatus` — owned pointer status (`int *_Owned`)
- `SStatus` — struct-with-owned-fields status (e.g. `struct { int *_Owned p; }`)
- `BOPStatus` — borrow-with-owned status (parameter passed by `_Borrow` to an owned ptr)

Plus per-VD sets: `*AllOwnedFields`, `*OwnedOwnedFields`, `SNullOwnedFields`.

## Functions

### `IsTrackedType` — :58-69
**Invariant**: returns true iff a type participates in ownership tracking.
**Branches**: owned pointer type, owned struct type, struct with owned fields.
**Note**: typedefs handled via `getCanonicalType()` at call sites — caller's responsibility.

**[2026-05-30 Chain-O recursion-depth audit — PROBED-folded-into-F80]** Case 3
(record) gates on `type->isMoveSemanticType()` — the SAME shallow helper
`isMoveSemanticTypeImpl` (TypeBSC.cpp:341-367) that F80 hit at the global decl gate.
`isMoveSemanticType` walks a record's FIELDS but does NOT walk a field's pointer-POINTEE
chain. So a function-LOCAL of type `struct O { int *_Owned * f; }` (owned one plain-pointer
deep in a field) is NOT tracked → the function-CFG leak/double-free check NEVER fires.
- Proven asymmetry: local `struct O { int *_Owned f; } o = make(...);` (DIRECT owned field)
  → REJECTED `field memory leak of value: o, o.f is leak`; local `struct O { int *_Owned * f; }`
  (owned one pointer deep) → ACCEPTED (exit 0). Runtime (heap-holder, `_Unsafe`): ACCEPTED,
  valgrind `definitely lost 8 + indirectly lost 4` — silent leak FN.
- **FOLD verdict (NOT a new filing):** this is a DIFFERENT call-site (Analysis dataflow
  `IsTrackedType` :59 vs F80's Sema decl gate `CheckOwnedOrIndirectOwnedType` :122) and a
  different observable (function-body leak vs global-decl rejection), but the ROOT is the
  shared `isMoveSemanticType`-no-pointee-walk helper. F80's fix surface explicitly offers
  deepening `isMoveSemanticTypeImpl` to walk the field pointee chain — that one helper fix
  closes this site too. Same root-cause family / same fix surface = FOLD per campaign rule
  ("a different AST kind / call-site hitting the same shallow helper is NOT a distinct root
  cause"). Whichever F80 fix the maintainer picks, if it's the helper-deepening option this
  is auto-closed; if it's the local `:122 → hasOwnedFields()` option, THIS site should be
  flagged in the F80 fix review as a co-located peer needing the same change.
- BorrowChecker peer `IsTrackedTypeImpl` (BSCBorrowChecker.cpp:25-51) is RICHER: for an owned
  pointer it recurses into the pointee (:28-30), checks `isBorrowQualified()` at every reached
  level (:36), and recurses into ALL struct fields (:43-44). It does not need owned-leak
  tracking (borrow=lifetimes only). Not an owned-vs-borrow asymmetry bug per se.

### `OwnershipImpl::merge` — :209-309
**Invariant**: OR-joins per-VarDecl bit-lattices and field-sets from two CFG predecessor states. If `statsA.empty()` → adopt `statsB` wholesale ("block not yet visited").
**Peers**: every `is()`/`has()`/`canAssign()` consumes its output.
**Candidates**:
- **C5** Multi-bit states reachable by OR are not enumerated. Specifically: `Null|Owned` (one path null-assigns, other owns) → `is(VD, Null)` returns false, `is(VD, Owned)` returns false, `is(VD, Uninitialized)` returns false. Downstream `HandleDREUse` (:925-947) cascade falls through to no-diagnostic. Need to confirm no checker depends on Null-discrimination here.
- statsA.empty() check is "BV is empty" semantics? Inspect to confirm. If statsA has keys but BVs are zero-bit, behavior differs.

### `OwnershipStatus::is(VD, S)` — :321-356
**Invariant**: returns true iff status bits for VD == singleton {S}. Tests bit S, resets it, returns `!any()`.
**Peers**: `has(VD, S)` is the relaxed form (bit S set, *plus* other bits).
**Candidate**: any downstream code that uses `is()` where `has()` is correct (or vice versa) on multi-bit merged states.

### `OwnershipStatus::canAssign(VD)` — :395-x
**Invariant**: returns true iff VD's status implies "no leak if reassigned" — only Uninit/Moved/Null bits set.
**Used by**: `checkMemoryLeak` (:1913, :1972), `assign` paths (:1088, :1638).
**Candidate**: `Null|PartialMoved` reachable? `canAssign` resets Null+Moved+Uninit; PartialMoved bit remains → returns false → flagged as leak. But is that intended? If a path is "null then moved partial" the leak diagnostic may misfire (false positive).

### `setToOwned` — :697-715
**Invariant**: resets all bits, sets Owned bit, marks all owned fields as still-owned.
**Caller-side**: called from DeclStmt init (:2326,:2330,:2345), BinAssign to non-null source (:950).

### `setToAllMoved` — :717-737
**Invariant**: resets all bits; if VD has owned fields, sets `AllMoved` and clears owned fields; otherwise sets **Owned**.
**Surprise**: for a plain owned pointer with no owned fields, "AllMoved" is the **same as Owned**. Name suggests "moved out from" but actual use is "received ownership from a void* cast" (taken from raw).
**Callers**: BinAssign with `IsCastFromVoidPointer(RHS)` (:2167); DeclStmt init with same (:2324).
**Candidate**: post-merge `AllMoved|Owned` state has no special handler — `HandleDREUse` (:925-947) falls through silently. Need adversarial input where a path sets AllMoved via void*cast and another path sets Owned via direct alloc; subsequent use.

### `VisitDeclStmt` — :2311-2354
**Invariant**: for each VarDecl with init, classify init form and set initial status; if tracked, recurse on init expr with `op=Move`.
**Branches on init form**:
1. owned pointer + null → `setToNull`
2. owned pointer + `IsCastFromVoidPointer` → `setToAllMoved`
3. owned pointer + else → `setToOwned`
4. record with owned fields + `isa<InitListExpr>(Init)` → `setToOwned` then `HandleInitListExpr`
5. else → `setToOwned`
**C1 — CONFIRMED 2026-05-19 as F17 (IJOERU)**: `isa<InitListExpr>(Init)` does NOT strip wrappers. A C99 compound literal `(struct S){.f = nullptr}` is a `CompoundLiteralExpr` (not `InitListExpr`); `HandleInitListExpr` is skipped → all fields coarsely marked Owned → null-init fields wrongly leak at scope-exit. Fix: strip `CompoundLiteralExpr` via `getInitializer()` before the isa check.

### `HandleInitListExpr` — :2356-2394
**Invariant**: walks init list field-by-field; for known sub-init shapes (null, nested init list, void* cast), update per-field owned/null sets.
**Branches**: `isNullExpr`, `dyn_cast<InitListExpr>(FieldInit)`, `IsCastFromVoidPointer(FieldInit)`.
**C1 — CONFIRMED 2026-05-19 as sibling site of F17 (IJOERU)**: `dyn_cast<InitListExpr>(FieldInit)` doesn't unwrap `CompoundLiteralExpr`. A nested compound-literal field init (`{.i = (struct Inner){.g = nullptr}}`) bypasses the recursive nested handling → inner field `o.i.g` wrongly tracked as owned → spurious `field memory leak`. Folded into F17's repro as `v_nested_compoundlit`. Fix must address both this site **and** the parent site at :2336.

### `VisitInitListExpr` — :2396-2402
**Invariant**: visits each init as a Move. Naive.
**Candidate**: top-level InitListExpr is visited but nested ones via `HandleInitListExpr` are not — asymmetry. Init list inside an init list: outer's `HandleInitListExpr` recurses without calling `Visit` on the field expr → may miss inner side-effects from CallExpr field inits?

### `VisitReturnStmt` — :2404+
**Invariant**: handles return-of-void*-cast specifically (diagnoses `PassCastToArgOrRet`); other returns fall through to generic Visit recursion.
**Candidate**: needs full read; partially covered.

### `HandleDREUse` — :~870-963 (use of a DRE to an OPS VD)
**Invariant**: emits a diagnostic if status isn't exactly Owned; if addr-mut and status is Null, "promotes" to Owned (line 950); if not getAddr, sets to Moved/AllMoved post-use.
**Diagnostic cascade** at :925-947: `Moved` → `Uninit (is)` → `Uninit (has)` → `PartialMoved (is)` → `AllMoved (is)`.
**Candidate (C5)**: multi-bit merged states with no clean fall-through:
- `Owned|AllMoved` — both bits set; `is()` calls return false; no diagnostic. Is the bookkeeping `OPSOwnedOwnedFields[VD].clear()` at :955 enough to preserve correctness on next use? Worth probing.
- `Null|Owned` — same.

### `checkMemoryLeak` — :1905+
**Invariant**: at LifetimeEnds, if `canAssign(VD)` false → emit MemoryLeak diag and reset to Moved (so subsequent iterations of for/while don't re-fire). Plus per-field leak for SStatus.
**Special-case**: `isDestructor && isa<ParmVarDecl>` (:1941) — only path that emits `OwnedStructNotProperlyFreed`. Asymmetric.
**Candidate (C4-ish)**: outside destructors, partial-move of param is permitted silently? Read further to confirm.

### `initOPS` / `initS` — :459-601
**Invariant**: at struct/owned-pointer decl, builds the per-VarDecl field-name maps. At top-level (depth==10), sets `OPSStatus[VD]` or `SStatus[VD]` to `BitVector(7, 0)` and `set(VD, Uninitialized)`.
**Asymmetry observed (F19 root)**: the **initial state is Uninitialized**, but the FIELD assignment handlers (`checkSFieldAssign` at :1419, `checkSAssign` at :1382) update only the per-field SOwnedOwnedFields set — they NEVER call `set(VD, Owned)` or `resetAll`. So `SStatus[VD]` remains Uninitialized after a successful field assign. Subsequent `checkSFieldUse` (:1308) sees `is(VD, Uninitialized)` true and emits "use of uninitialized value: VD.field".

### `checkSFieldAssign` — :1419-1510 (F19 surface)
**Invariant should be**: a successful field assignment marks the parent struct as initialized (Owned if all fields covered, PartialMoved otherwise).
**Actual behavior**: only updates `SOwnedOwnedFields[VD]`. Does NOT clear the Uninitialized bit on `SStatus[VD]`. Filed as **F19 (IJOFP7)**.
**Fix surface**: after lines 1502-1507, add `resetAll(VD); set(VD, SOwnedOwnedFields[VD].size() == SAllOwnedFields[VD].size() ? Owned : PartialMoved);`.

### `checkSAssign` — :1382-1416 (F19 surface, same root)
**Invariant should be**: whole-struct assignment promotes the struct to Owned.
**Actual behavior**: updates `SOwnedOwnedFields[VD]` at line 1414. Does NOT clear Uninitialized bit. Same F19 root.

### `checkOPSFieldAssign` — :1151+ (asymmetric with checkSFieldAssign)
**Invariant**: when assigning a field through a `_Owned` pointer (`p->f = X`), check that the parent pointer is Owned. Lines 1192-1196 explicitly check `is(VD, Uninitialized)` and emit `InvalidAssignFieldOfUninit` — REJECT the write.
**Comparison with checkSFieldAssign**: OPS REJECTS the write (correct given the pointer must be init to deref). S ACCEPTS the write but later REJECTS the read (F19). Either is defensible; current behavior is *inconsistent* between the two.

## Candidate status (ranked, with progress)

1. **C5/merge** — `Null|Owned` (or `AllMoved|Owned`) post-merge state: **UNPROBED** (low priority — hard to construct exploit; nullability check catches deref independently). Speculative.
2. **C1/InitList paren-wrap** — `((struct S){...})`: **UNPROBED** — but C1 class is CONFIRMED at this site via F17. Variant; do not probe.
3. **C1/VisitDeclStmt** isNullExpr/IsCastFromVoidPointer wrapper-handling — **PROBED-INCONCLUSIVE 2026-05-19** — both helpers handle wrappers correctly.
4. **C1/CompoundLiteralExpr in RHS of `s = ...`** — assign-time tracking: **PROBED-INCONCLUSIVE 2026-05-19** — `assign to _Owned value` check fires before per-field bookkeeping matters.

## Not yet read (high priority)

- `VisitBinaryOperator` — assign cases READ; non-assign cases unread
- `VisitCallExpr` (`isHandlingCallExpr` flag and arg-walk) — READ
- `VisitMemberExpr` (deref through `.`/`->`)
- `VisitCStyleCastExpr` / `VisitImplicitCastExpr` (the cast handling) — partially READ via L2236+
- All the per-FieldDecl helpers
- ~`HandleDREAssign` (line 2431)~ — READ 2026-05-20

### `HandleDREAssign` — :2431-2502
**Invariant**: dispatches an assignment LHS DRE to the appropriate per-source check (OPS / S / BOP), based on `fullFieldName` form ("", "*", trailing-"*", or plain field path).
**Peers**: `HandleDREUse` (parallel structure for reads), per-source `check[OPS|S|BOP][Assign|FieldAssign|DerefAssign]`.
**Suspicious code (lines 2452-2459, 2474-2481)**: when `fullFieldName` ends with `*`, the handler:
  1. Temporarily replaces the trailing `*` with `.`
  2. If the truncated-by-1 name (e.g., `f*` → `f`) is in the `*AllOwnedFields` set, calls `checkXxxFieldAssign` with the *dotted* form (`f.`)
  3. Restores the `*`
  4. Calls `checkXxxFieldAssign` again with the original `*`-form (`f*`)
  
  This dual-call with two different name representations of the SAME logical operation is asymmetric: the dotted form might match prefix logic (which uses `.`-separated paths) while the star form matches the deref-chain logic. **Candidate (C5)**: the two calls may double-count moves, leading to false-positive partial-move detection on consecutive nested-pointer field writes.
**Candidates ranked**:
1. **C5 dual-call duplication on `*`-trailing field names** — UNPROBED. Try `*s.f = X` where f is doubly-owned: does the analyzer over-trigger?
2. **C1 fullFieldName transformation in-place** — the `fullFieldName[size-1] = '.'` mutates the input string, restored at line 2459/2481. If `checkXxxFieldAssign` stores the string anywhere (e.g., in a diagnostic), it'd see the dotted form. Not exploitable but fragile.
3. **C3 BOP handling at line 2490** — only handles OPSStatus/SStatus/BOPStatus mutually exclusively per VD. Can a VD ever be in multiple status maps?

### `OwnershipImpl::merge` — :209-300 — READ 2026-05-20
**Invariant**: per-predecessor join of OPSStatus, OPSAllOwnedFields, SStatus, SAllOwnedFields, BOPStatus, etc. Same asymmetric pattern as `mergeVD`/`mergeFP`/`mergeDPVD` in BSCNullabilityCheck (the F26 root cause):
- For each VD in B's map: if A has VD → OR the BitVectors (may-analysis); else set A[VD] = B's value.
- **Bug surface**: when A's path doesn't track VD (absent from map) and B has VD with state S, A becomes B[VD]=S. Downstream checks see the var as state S even on paths where it wasn't reached.
**Why F26 doesn't directly apply here**: BSCOwnership's per-VD tracking is keyed on VarDecl declarations. Once a var is declared (VisitDeclStmt initializes its entry), all subsequent CFG paths inherit the entry through merges. The "A doesn't have VD" case happens only when one branch reaches the declaration but another doesn't — i.e., when the declaration is INSIDE a conditional block.
**Candidates**:
1. **Var declared inside one branch but not the other; merge confused state** — UNPROBED. Possibly unreachable since the var goes out of scope at the end of the inner block (StorageDead clears the map entry?). Need to verify.
2. **Field maps (SAllOwnedFields, OPSAllOwnedFields) — similar asymmetry** — UNPROBED. These tracking maps may inherit from B without symmetric meet.
3. **Cross-scope VD with same name shadowing** — already probed (probe_shadow.cbs); per-VarDecl identity prevents confusion.

## Cycle 7-8 reading: setToOwned, setToAllMoved, OwnershipImpl::merge OPS branch

### setToOwned (line 697-715) / setToAllMoved-by-VD (line 717-737)

**Invariant**: Three parallel branches for OPS / S / BOP. setToOwned: resets all status bits and re-sets Owned, copies AllOwned→OwnedOwned. setToAllMoved-by-VD: resets, sets AllMoved (if AllOwned non-empty) or Owned (if no fields).

**Status**: parallel structure looks consistent; tested via /tmp/probe_multi_owned_asym.cbs (CLEAN). No probe-worthy gap.

### setToAllMoved(const Expr *E) (line 739+)

**Invariant**: dispatches on Expr kind:
- DRE: setToAllMoved by VD.
- MemberExpr: getMemberFullField → DRE base → mark field as moved across three branches (OPS / S / BOP).

**Reachability**: only handles DRE and MemberExpr. Other expression forms (deref-of-member, complex paths) fall through silently. Probably by-design — those aren't typically move sources.

### OwnershipImpl::merge OPS typo (line 232/240)

**Re-audit confirmed**: `statsA.OPSAllOwnedFields[VD] = statsB.OPSOwnedOwnedFields[VD]` assigns currently-owned into all-owned in the else branch. Hard to construct a triggering shape; OwnershipStatus init pre-populates both fields consistently before merging is needed. DEFENSIVE-CODE-INCONSISTENCY persists from cycle 9 — INCONCLUSIVE on actionable repro.

## Cycle 8: initOPS / initS (line 459-602)

**Invariant**: traverses a struct's fields and populates `OPSAllOwnedFields[VD]` (or `SAllOwnedFields[VD]`) with transitively-reachable owned-field paths. Recursion limit depth=10.

**Reachability candidates**:
- Depth 10 limit (line 519) — structs nested >10 levels would lose field tracking. **Unrealistic in practice; not probe-worthy.**
- No cycle detection — for self-referential `struct A { struct A* a; };` recursion goes A→A→... down to depth 0. Depth limit prevents infinite loop. By-design protection.
- Line 487 handles `_Owned`-pointer-to-record by recursing on pointee. For non-record pointees (e.g. owned-int pointer), drops to initBOP (line 501).
- Line 504-515 handles record fields (non-pointer) by recursing initS.

**Status**: structurally sound for in-scope shapes (struct with `_Owned` pointer fields, nested non-_Owned structs). No new defect candidate identified.

## Cycle 9: TransferFunctions::VisitArraySubscriptExpr, VisitMemberExpr

### VisitArraySubscriptExpr (line 2044-2066)

**Invariant**: walk nested ASE collecting "*" suffix; if base is MemberExpr rooted in DRE → VisitDeclRefExpr with field-path+suffix; if base is DRE → VisitDeclRefExpr with suffix; else recurse on base.

**Reachability**: when base is a complex expression (e.g. another ASE, function call, etc.) that doesn't resolve to a DRE-rooted MemberExpr or pure DRE, falls to line 2065 (Visit base). For in-scope shapes (arrays of _Owned rejected at type level), this path can't carry _Owned tracking; fall-through is benign.

### Saturation note

Cycles 2-9 produced no new defects in this session. F39 (cycle 1) was the productive find. The defect frontier within in-scope features (`_Owned`, `_Borrow`, `_Safe`/`_Unsafe`, init analysis, nullability) has reached high saturation: candidates either fold into existing F-numbers, are blocked by type-level SHAPE-REJECTED rules, or are unreachable from valid BSC programs.

## checkSUse (BSCOwnership.cpp:1268-1306) — PROBED-confirmed-F44

**Invariant**: when struct VD is used, validate per-field state (no use of moved/uninit/partially-moved) AND for `&_Mut` mutable borrows, normalize per-field state for downstream analysis.

**Peers**: `checkOPSUse` (line 919+), `checkBOPUse` (line 1513) — all three have parallel `isAddrMut` blocks. `checkSFieldUse` (line 1308) shares the SAllOwnedFields/SOwnedOwnedFields/SNullOwnedFields three-set model.

**Probed candidate**: `isAddrMut` branch at line 1296-1302 collapses `SNullOwnedFields[VD]` into `SOwnedOwnedFields[VD]` and clears the null set. This treats `&_Mut` as "field IS now Owned" rather than "field MAY be Owned or Null". Downstream `checkMemoryLeak` then iterates SOwnedOwnedFields and flags every field as a leak.

**Filed as F44 (IJOSGF)**: `struct S s = {.b = nullptr}; taker(&_Mut s);` → false-positive leak diag at scope exit. Cross-analyzer mismatch: nullability analyzer correctly preserves Null state through `&_Mut`, so an unconditional `safe_free((void *_Owned)s.b)` is still rejected — forcing the user into `if (s.b != nullptr) safe_free((void *_Owned)s.b)` defensive code.

**Sibling sites NOT probed (out of scope for campaign)**: `checkOPSUse`/`checkBOPUse` `isAddrMut` blocks gate `_Owned struct *_Owned` / `_Owned struct *_Borrow` paths; same collapse pattern, same fix surface but feature combos outside scope. Fix surface is parallel.

## checkSFieldAssign (BSCOwnership.cpp:1419-1510) — PROBED-confirmed-F45 (HIGH)

**Invariant**: when field `VD.fullFieldName` is assigned, validate ownership preconditions AND update the per-field state containers to reflect the assignment.

**Peers**: `checkOPSFieldAssign` (line 1151+), `checkBOPFieldAssign` (line 1665+), `setToAllMoved(Expr*)` (line 768-787 SStatus branch), `setToNull(Expr*)` (line 826-836 SStatus branch). The last two are the lockstep peers that demonstrate the correct discipline: erase from one set, insert into the other.

**Filed candidate**: lines 1502-1507 insert into `SOwnedOwnedFields[VD]` but do NOT erase from `SNullOwnedFields[VD]`. When a previously-null field is reassigned to a non-null value, both per-field containers end up containing the field — an internally inconsistent state. The stale Null entry persists until something else erases it.

**Soundness consequence (filed F45 IJOSHF, HIGH)**: combined with `checkSUse`'s `isAddrMut` migration (F44 IJOSGF), the stale entry is "restored" to the Owned set after the field has been moved out. The analyzer then accepts a second cast-and-move of the same field, producing a runtime double-free (valgrind 1 alloc/2 frees, Invalid free).

**Sibling sites NOT confirmed buggy**: `checkOPSFieldAssign` and `checkBOPFieldAssign` don't have an OPSNullOwnedFields/BOPNullOwnedFields equivalent — only SNullOwnedFields exists in the codebase — so the same omission pattern doesn't immediately translate to those branches.

**Fix surface**: at line 1503 and within the prefix loop at 1505-1507, mirror the lockstep pattern: every `SOwnedOwnedFields[VD].insert(x)` should be paired with `SNullOwnedFields[VD].erase(x)`.

## getMemberFullField (BSCOwnership.cpp:92-109) — PROBED-confirmed-F46 (HIGH)

**Invariant**: given a MemberExpr, return the underlying base Expr (a DRE if the access chain bottoms out at a variable) and the dot-joined field path string. Used by VisitMemberExpr/VisitCStyleCastExpr/setToNull/setToAllMoved to identify the VarDecl whose per-field state to update.

**Peers**: callers gate every state update on `dyn_cast<DeclRefExpr>(memberField.first)` — so the function's promise is "return a DRE when the access bottoms at a variable; otherwise return something that signals 'unhandled'."

**Filed candidate**: traversal loop strips `ImplicitCastExpr` (line 101) but NOT `ParenExpr`. When the user writes `(s).b`, ME's base is a ParenExpr; the loop falls through immediately; downstream dyn_cast<DRE> fails; the move/use/cast bookkeeping is silently skipped.

**Soundness consequence (filed F46 IJOSI3, HIGH)**: pure `_Safe` code using `(int *_Owned)(s).b` to move a field, then `safe_free((void *_Owned)q)`, then `safe_free((void *_Owned)s.b)` — both safe_frees accepted by the analyzer; valgrind reports invalid free. No GNU extensions, no out-of-scope features.

**Fix**: add `else if (const ParenExpr *pe = dyn_cast<ParenExpr>(base)) { base = pe->getSubExpr(); continue; }` to the while loop. Or set `base = base->IgnoreParenImpCasts()` at function entry. One-line fix closes every paren-wrap variant.

**Other ME base-wrapper variants NOT separately filed (same root cause)**: `((s)).b` double paren; `(*(&s)).b` UnaryOp base; macro expansions producing `((arg)).field`.

**Defect class**: C1 (Ignore-asymmetry / wrapper-skip). Sibling of F14, F21, F36.

## Cycle 2026-05-21: VisitReturnStmt / VisitCallExpr / OwnershipImpl::merge S-branch — DEEPER READ

### VisitReturnStmt (:2404-2422) — READ

**Invariant**: at a ReturnStmt CFG element, set `op = Move` for non-integer returns (op=GetAddr if integer-typed return), then `Visit(RV)` which dispatches to the appropriate Visit* per RV's Stmt class.

**Peers**: VisitCallExpr (:2207), Sema's CheckMoveVarMemoryLeak / CheckTemporaryVarMemoryLeak (filed exemplars F21, F25, F33), Sema's CheckReturnStmtMemoryLeak (SemaBSCOwnedStruct.cpp:37 — only fires for `_Owned struct` types, OUT OF SCOPE).

**Probed candidates (2026-05-21)**:
1. **Return `(struct S){.f = mk()}`** (compound literal with owned-field RV) — **INCONCLUSIVE-CLEAN**. valgrind 1/1, no leak. Analyzer correctly tracks move into return value (via parent VisitDeclStmt setToOwned at the caller's decl init).
2. **Return `(struct S){.f = nullptr}` and discard at caller** — **CORRECTLY-DIAGNOSED**. Analyzer emits field memory leak diag because caller's `struct S s = producer();` falls through to setToOwned (line 2345) — all fields marked Owned regardless of producer's runtime value. Conservative-but-sound false positive on runtime-null path.
3. **Return ternary `c ? (struct S){.f = mk()} : (struct S){.f = nullptr}`** — FOLDED-F22 if mk() path runs without caller free; otherwise conservative-tracked.

### VisitCallExpr (:2207-2227) — READ

**Invariant**: when `isHandlingCallExpr` (set only for top-level CallExpr CFG elements), iterate args and `Visit(Arg)` with `op = Move` (or `GetAddr` for _Bool params). Args not consumed otherwise.

**Note**: `isHandlingCallExpr` is reset to false after first call; never re-set during recursion. So nested CallExpr inside an arg (e.g., `f(g())`) doesn't trigger VisitCallExpr's arg-walk on g — g is just Visited as a sub-expression. This is fine because g's args are still side-effected; the gap would be in tracking g's *result* as a move source.

**Probed candidate (2026-05-21)**:
- **Passing `mk()` to a `_Bool` parameter** — **SHAPE-REJECTED** by Sema's `isOwnedQualified -> _Bool` "forbidden conversion" check at the safe-zone gate. Cannot reach the analyzer.

### OwnershipImpl::merge S-branch (:254-276) — DEEPER READ

**Suspect observation**: lines 271-274 in the "case 2" branch (when statsA lacks the VD or has empty SOwnedOwnedFields/SNullOwnedFields globally) write BOTH `statsB.SOwnedOwnedFields[VD]` AND `statsB.SNullOwnedFields[VD]` into `statsA.SAllOwnedFields[VD]`. This conflates the all-fields tracker with currently-owned/currently-null subsets.

**Plus**: case 2 does NOT populate statsA.SOwnedOwnedFields[VD] or SNullOwnedFields[VD] — both remain empty. Subsequent uses on the merged state would lookup empty containers.

**Reachability analysis**:
- statsA.SAllOwnedFields.count(VD) == 0 requires statsA to have not seen the VD's initS yet. This means the decl is on a CFG path that statsA didn't reach.
- For a normal `_Safe void f() { ...; if(c) { struct S s; ...} ...; }`, s is declared inside the if; the var goes out of scope at the end of the if block — LifetimeEnds + checkMemoryLeak fire; subsequent merge after the if won't have the VD live.
- **Conclusion**: case 2 is structurally unreachable from valid in-scope BSC. The "polluted SAllOwnedFields" cannot be observed by downstream checks because the VD is out-of-scope at the merge point.

**Probed asymmetry**: assigned via 7 probes in this cycle; all either FOLD into existing Fxx or are SHAPE-REJECTED.

### setToNull(Expr*) BOP-branch missing NullOwnedFields tracking (:856-878)

**Asymmetry**: the S-branch (line 826-836) tracks per-suffix null state via `SNullOwnedFields`; the BOP-branch (line 856-878) does NOT — it only erases from BOPOwnedOwnedFields and sets `PartialMoved` / `AllMoved`. After `*b = nullptr;` where `b: int *_Owned *_Borrow`, the inner is treated as Moved, not Null.

**Why this doesn't manifest as a defect**: assigning null through a `_Borrow` (i.e., `*b = X` where b is the borrow) is rejected by Sema for owned types (cannot drop ownership through borrow). Probed via /tmp/owen_probe_bop_null_*.cbs — all SHAPE-REJECTED at the move-through-borrow gate (well-known limitation, F34 territory). **No reachable exploit.**

### setToNull(Expr*) S-branch `*`-suffix prefix-walk missing (:830-835)

**Same root cause as F23 (HandleInitListExpr at :2368-2373)**: only walks `.`-prefixes, not `*`-prefixes. The assign-time path has the same bug at a parallel call site.

**Probe (2026-05-21)**: `struct S s; s.f = nullptr;` for `struct S { int *_Owned _Nullable *_Owned _Nullable f; }` reproduces the same `*s.f is leak` false positive (`/tmp/owen_probe_assign_path_doublestar.cbs`). **FOLDED-F23**: same root cause, parallel fix surface (lines 830-835 mirror the lines 2368-2373 pattern). Single conceptual fix at the prefix-walker would close both.

### Designated-init override `{.f = mk(), .f = nullptr}` — clang warning suffices

**Probe**: clang emits `-Winitializer-overrides` with explicit "side effects will not occur at run time" note. Runtime: 0 allocs (mk() elided). No leak — the warning is the only diag needed. Not a defect.

## VisitDeclStmt else-branch DRE-init (BSCOwnership.cpp:2344-2346) — PROBED-confirmed-NEW (2026-05-21)

**Invariant should be**: when declaring `struct S t = src;` where `src` is a DeclRefExpr to another struct, `t` should inherit `src`'s per-field state (specifically the `SOwnedOwnedFields` / `SNullOwnedFields` partition).

**Actual behavior**: VisitDeclStmt line 2344 `else` branch unconditionally calls `setToOwned(VD)` (line 707). `setToOwned` sets `SOwnedOwnedFields[t] = SAllOwnedFields[t]` (coarsely Owned for all fields). It does NOT copy `SNullOwnedFields[src]` over to `SNullOwnedFields[t]`. At scope-end, `checkMemoryLeak` (line 1932-1939) sees `SOwnedOwnedFields[t]` non-empty → fires `FieldMemoryLeak` diag.

**Peers**:
- VisitDeclStmt line 2328-2342 (InitListExpr branch) — DOES propagate per-field null state via `HandleInitListExpr`. The asymmetry: structurally-equivalent InitListExpr-init is handled correctly; DRE-init is not.
- `checkSAssign` (line 1382-1414) for whole-struct REASSIGNMENT (`t = s` post-decl) — same coarse `SOwned = SAll` at line 1414, also doesn't copy SNull. Likely same root.
- `setToOwned(VD)` (line 707) — the actual mechanism. Used by every "VD is fully Owned now" code path. May be the root fix surface.

**Probe (2026-05-21)**:
- Repro: `struct S s = {.b = nullptr}; struct S t = s; (void)t; return 0;` → **`error: field memory leak of value: 't', t.b is leak`**. False positive — runtime t.b is null, nothing to leak.
- Baseline: replace `struct S t = s;` with `struct S t = {.b = nullptr};` → **clean compile**.
- Non-null sanity: `struct S s = {.b = mk()}; struct S t = s; if (t.b!=null) safe_free(...);` → clean (1 alloc / 1 free). Confirms the defect is specifically the null-init → DRE-copy path.

**Severity**: MEDIUM (false positive; rejects legitimate `_Safe` code; workaround: avoid the intermediate copy or restructure).

**Defect class**: C5 sibling — per-field state container loses precision at a transition point. Same class as F44 (Null→Owned collapse on `&_Mut`), F45 (stale SNull after reassign), F19 (no SStatus update on field-assign). Different code site: this fires at the DECL of the destination, not at a use or assign.

**Distinct from existing F-numbers**:
- F17 (IJOERU): VisitDeclStmt:2336 `isa<InitListExpr>(Init)` wrapper-miss for CompoundLiteralExpr. Different shape (wrapper-strip needed vs DRE-init never reaching the per-field handler). **Both surfaces transit through the same `else` branch at :2344 with the same defective `setToOwned`**, but F17's proposed fixes (strip CompoundLiteralExpr / add CLE-recognizer branch) DO NOT close the DRE-init case. The DRE shape requires a different fix: propagate source struct's `SNullOwnedFields` (and related per-field state) to destination when Init resolves to a DeclRefExpr of another struct-with-owned-fields.
- F44/F45: `&_Mut`-driven collapse. Different trigger (no `&_Mut` here).
- F47: CompoundLiteralExpr-as-MemberExpr-base. Different shape.
- Prior probe at `_probed.md:474` (`struct WithOwned s2 = s1;`) — used non-null-init field, marked CLEAN. The null-init variant is a distinct exploit.

**Fix surface**: VisitDeclStmt line 2344-2346 needs to recognize "Init is DRE/ME/etc. to another struct-with-owned-fields" and propagate the source's per-field state to the destination. Alternative root fix: `setToOwned(VD)` could accept an optional source-VD argument and copy SNull from source. Either approach extends to `checkSAssign` line 1414 too (whole-struct reassign).

## __attribute__((cleanup)) blindness — PROBED-confirmed-F72 (2026-05-29)
The ownership dataflow doesn't model `__attribute__((cleanup(fn)))` as a scope-exit
consumption. `int *_Owned p __attribute__((cleanup(c))) = mk();` → FP "memory leak"
when cleanup is sole disposer; adding `consume(p)` to silence it → compile-clean
double-free (valgrind 1 alloc/2 frees) because the synthesized `c(&p)` at scope exit
is invisible to consume-tracking. Control (double explicit consume) IS caught. MEDIUM,
filed. repro/F72_cleanup_attr_owned_double_free.cbs.

## OwnershipImpl::merge (BSCOwnership.cpp:209-303) — 2026-05-29
**Invariant**: CFG-join merge of ownership state; status bitvectors OR'd (may-analysis);
field-sets (SAllOwnedFields/SOwnedOwnedFields/SNullOwnedFields) unioned per-VD.
**Peers**: F35 (goto-skip: VD-in-B-not-A → takes B's Owned), F44/F45 (SNullOwnedFields
collapse/stale). Iterates statsB only → VD-in-A-not-B keeps A's state (F35 asymmetry).
**Candidates**:
1. **Line 261-262 map-vs-set emptiness**: condition checks `statsA.SOwnedOwnedFields.empty()`
   (WHOLE MAP) not `statsA.SOwnedOwnedFields[VD].empty()`. If statsA's owned/null maps are empty
   (no tracked var has owned/null fields) while merging a VD with owned/null fields from B → else
   branch (271-276) records fields only in SAllOwnedFields, DROPPING owned/null discrimination →
   field becomes neither-owned-nor-null. Possible FP-uninit or owned-when-null. **PROBED-NOT-TRIGGERED 2026-05-29**: owned field moved on one branch / owned on other → analyzer correctly flags `s.a is leak` at scope exit (discrimination survived the merge). Could not construct a scenario where the map-vs-set condition produces an FN/FP. Real code smell, not reachable into a defect (cf. RecursiveForFields latent). Likely F44/F45-adjacent if ever reachable.
2. Status OR Owned|Moved at join — conservative if checks read Moved bit; FN if read Owned. (F08 area.)
3. VD-in-A-not-B keeps A state (F35).

## OwnershipImpl::merge field-set UNION vs status-OR asymmetry (BSCOwnership.cpp:254-276) — 2026-05-29 (C5 join-shape hunt)
**Invariant**: at a CFG join, the per-field owned/null/all sets (SOwnedOwnedFields/SNullOwnedFields/
SAllOwnedFields) are UNION'd across predecessors, while the SStatus bitvector is OR'd. For LEAK detection
this is conservative (union keeps a field "owned" if owned on ANY path → flagged). The open question (C5):
is it conservative for the DUAL direction — a field MOVED on one path, OWNED on the other, then CONSUMED
unconditionally after the join? The union restores the field to SOwnedOwnedFields (owned on path B) → a
post-join `consume(s.f)` sees the field Owned → ACCEPTED. On path A (where the field was already moved),
that consume is a SECOND free → runtime double-free FN.
**Peers**: VisitAbstractConditionalOperator (:2187 in-block merge), the worklist join (:2962-2968), F67
(deref-of-moved-field — that fires on a deref USE, not a re-consume after merge), F45 (stale SNull restore
via &_Mut — different trigger), checkMemoryLeak (:1932 reads SOwnedOwnedFields for leak).
**Candidates**:
1. **C5 FN — struct field moved in ONE if-branch, consumed unconditionally after join** —
   **CONFIRMED-NEW 2026-05-29 (double-free FN; pending file).** Root cause lines 262-265:
   `SOwnedOwnedFields[VD]` is UNION'd across predecessors (no meet/intersection). A field moved on the
   then-path (erased from the set) but owned on the else-path is RESTORED to owned by the union. A post-
   join `consume(s.p)` then sees the field owned → ACCEPTED → on the then-path that is a 2nd free.
   PROBE `/tmp/explorer_probe.AR4NLu.cbs`: ACCEPTED, valgrind 1 alloc / 2 frees / Invalid free.
   ASYMMETRY: BASELINE (unconditional double-consume) REJECTED; B2 (move on BOTH branches + post-consume)
   REJECTED; B3 (balanced 1-consume/path) clean; B4 (line-2307 shape: asymmetric move + scope exit, no
   post-consume) flags LEAK — proving the union keeps the field owned post-join. DISTINCT from the
   _probed.md:2307 probe which stopped at "leak correctly flagged" and missed the dual-direction
   double-free FN. DISTINCT from F67 (deref-site missing field-move check, no merge needed). Fix: the
   consumable-owned field set must be MEET (intersection) across predecessors, not union — a field is
   safely consumable post-join only if owned on ALL predecessors.
2. C5 FN — switch fallthrough: field moved in case 1, falls through to case 2 which also consumes. UNPROBED.
3. plain owned-pointer (OPS) version: moved in one branch, consumed after — but OPSStatus is OR'd and
   checkOPSUse reads has(Moved) → likely caught (notes: simple if/else balanced is sound). Lower priority.

## runOnBlock CFG-element filter (BSCOwnership.cpp:2603-2638) — Chain K analysis, 2026-05-30

**Invariant**: ownership runs on a CFG built with `setAllAlwaysAdd()` over the RAW AST
(SemaDeclBSC.cpp:273, full linearization — every Stmt class gets its own CFG element).
`runOnBlock` iterates each block's CFGElements but only calls `TF.Visit` on elements whose
`getStmt()` is one of **5 kinds** (lines 2615-2620): `DeclStmt`, `CallExpr`, an *assignment*
`BinaryOperator`, an *inc/dec* `UnaryOperator`, or `ReturnStmt`. For `CallExpr` elements it first
calls `SetHandlingCallExpr()` (else `VisitCallExpr` early-returns at :2208).

**Chain K question**: does the 5-kind filter + raw-AST CFG ever MISS a move/borrow that the borrow
Prologue CFG (10-class, on the Prologue-transformed AST) would have separated — a soundness FN?

**Answer: NO new root cause (Chain K SATURATED @ 28656aa9).** The `setAllAlwaysAdd` full
linearization is a strict SUPERSET: every nested side-effecting subexpr — a `consume()` call inside
a top-level `?:`/comma/if-condition, inside an assignment LHS or RHS, inside a `&&`/`||` operand —
becomes its OWN `CallExpr` (or `DeclStmt`) CFG element, so the 5-kind filter still reaches it even
though the *enclosing* statement (e.g. a bare `ConditionalOperator` or `IfStmt`) is NOT one of the
5 visited kinds. Verified by `-dump-owned-check`: `c ? consume(p) : consume(p);` yields per-arm
Moved status with a correct merge; `if (consume_ret(p)){consume_ret(p);}` rejects the 2nd consume;
`*p = consume_ret(p);` rejects the LHS deref-use; `c && consume_ret(p); consume_ret(p);` rejects via
MaybeMoved merge. All SOUND.

**The ONLY laundering construct is the comma operator = F11** (a borrow-checker-only
`ActionExtract`/`DefUse::VisitBinaryOperator` coalescing where `BO_Comma` falls through with no
operand visit). Ownership special-cases `BO_Comma` correctly (`VisitBinaryOperator` :2177), so the
comma MOVE-flow is sound — only the BORROW build is broken, and that is F11's single-build root, not
a divergence between the two CFG builds. `?:` and `&&` borrows ARE caught (Prologue hoists them):
`two((c,&_Mut x), &_Mut x)` accepted (= F11 comma), but `c ? &_Mut x : &_Mut x` + plain `&_Mut x`
correctly REJECTED. So Chain K adds nothing beyond F11; the two-build asymmetry is benign because
the raw-AST ownership/null CFG sees a superset of element boundaries.

## Chain U — parallel check-families matrix (BSCOwnership.cpp) — 2026-05-30

Read all 13 `check{S,OPS,BOP}{Assign,DerefAssign,FieldAssign,FieldUse,Use}` side by side +
both dispatchers `HandleDREAssign` (:2431-2502) / `HandleDREUse` (:2504-2578) + the
parameter/decl tracking gate.

**Family×op handler matrix:**
| op | S | OPS | BOP |
|----|---|-----|-----|
| Assign (`x=`) | checkSAssign :1382 | checkOPSAssign :1081 | checkBOPAssign :1633 |
| DerefAssign (`*x=`, fullFieldName=="*") | — (routes "*"→checkSFieldAssign) | **checkOPSDerefAssign :1115 (dedicated)** | — (routes "*"→checkBOPFieldAssign) |
| FieldAssign | checkSFieldAssign :1419 | checkOPSFieldAssign :1151 | checkBOPFieldAssign :1665 |
| FieldUse | checkSFieldUse :1308 | checkOPSFieldUse :966 | checkBOPFieldUse :1554 |
| Use | checkSUse :1268 | checkOPSUse :918 | checkBOPUse :1512 |

**Deref-of-moved-field (F67 class) symmetry:** OPS (:977-1002) and BOP (:1560-1569) BOTH have a
"check field's parent" prefix-walk loop that strips the `*`/`.` suffix and re-tests
membership → they CATCH a deref-of-moved (`*p.f` after move). checkSFieldUse (:1308) LACKS that
loop and uses only the literal `count(fullFieldName)` (:1335) → **that absence IS F67** (S-only gap,
filed). So the deref-of-moved check is symmetric for OPS/BOP, asymmetric only for S = exactly F67.
No NEW cell here.

**SNullOwnedFields asymmetry:** only the S family has a `*NullOwnedFields` set (checkSUse :1297,
checkSFieldUse :1336, checkSFieldAssign). OPS/BOP have none. That's the F44/F45 surface (S-only null
tracking via `&_Mut`). Not reachable as an OPS/BOP gap (no null-set to go stale).

**DerefAssign dispatch asymmetry (`*x = newval`):** OPS gets a DEDICATED `checkOPSDerefAssign`
(:1115) wired at HandleDREAssign :2447-2450 (`fullFieldName=="*"` branch). S and BOP have NO `"*"`
branch in the dispatcher — `"*"` falls into the `else` and routes to checkSFieldAssign / checkBOPFieldAssign
with fullFieldName=="*". For BOP the field model stores the one-deref slot as `"*"` in
BOPAllOwnedFields (initBOP :623), so checkBOPFieldAssign("*") line 1694 `count("*")`==true → the
overwrite-of-owned guard (line 1696 InvalidAssignFieldOfOwned) IS reachable IN PRINCIPLE — but only
if the var was TRACKED and the slot recorded owned. See the tracking-gate fold below: the typical
`_Borrow`-outer var is never tracked, so the guard never runs.

### FOLD into F34 — `int *_Owned *_Borrow b` param/local `*b = mk()` overwrite untracked (BOP sibling)
**PROBED 2026-05-30, FOLDED-into-F34.** `void put(int *_Owned *_Borrow b){ *b = mk(); *b = mk(); }`
compiles CLEAN; vg_probe = `definitely lost: 8 bytes` (both overwrites leak the slot's old owned
value). The "correct" pattern `safe_free((void*_Owned)*b); *b=mk();` is REJECTED ("_Borrow type does
not allow move ownership"), so the user has NO sound way to do this — the unsound overwrite is the
only accepted form. ROOT: identical to F34. `IsTrackedType` (:59) pointer branch (line 61) tests
`type.isOwnedQualified()` = OUTER qualifier only; outer is `_Borrow` → returns false. The
parameter-init gate (:2939 `if (IsTrackedType(PVD->getType()))`) AND the local-decl gate
(VisitDeclStmt :2315) both drop the var → never enters OPSStatus/SStatus/BOPStatus → HandleDREAssign
(:2440) finds no map entry → silent fall-through. F34's repro used a `_Borrow`-to-struct-with-owned-
fields (S shape); this is the NON-record (`int *_Owned *_Borrow`, BOP shape) sibling. SAME predicate,
SAME gate, SAME fall-through, SAME fix surface ("extend IsTrackedType / add parallel borrow-param
init path"). Per the strict-discipline rule (different type shape hitting the same predicate gap is
NOT a distinct root cause) → FOLD, do not file. Confirms F34's blast radius covers the BOP/non-record
nesting, not just records. Repro kept at /tmp/explorer_chainU_bop_double.cbs.

## R2 checkOPSUse-Null cell (BSCOwnership.cpp:925-928) — 2026-05-30 — CELL CLOSED (unreachable)

**The cell**: `checkOPSUse` (:925-928) only emits the `InvalidUseOfMoved` diag for a `Null`
status when `!OPSAllOwnedFields[VD].empty() && !isGetAddr` — i.e. for a PLAIN `_Owned` pointer
(no owned fields) a `Null` status is SILENTLY IGNORED at a use. **Parallel dead cell**:
`checkCastOPS` (:1743-1769) checks `Moved`/`Uninit`/`PartialMoved`/owned-fields but has NO
`Null` branch at all — casting a pure-`Null` plain owned to `void *_Owned` (= the `safe_free`
path) is accepted with no diag. Both are genuinely "Null-blind for plain owned."

**E1's hypothesis (2026-05-30): unreachable. R2 result: CONFIRMED unreachable — CLOSED.**
Tried HARD (8 probes, /tmp/probed_R2E3.md, repros /tmp/R2E3_p{1..8}*.cbs) to drive a plain
`_Owned` to **pure** `Null` status at a free/use while the **runtime** value is non-null
(dangling) → double-free FN. Every attempt is either SHAPE-REJECTED by the nullability checker
or SOUND. The trace proving unreachability:

- **Only `setToNull` makes PURE Null** (it does `resetAll(VD); set(Null)`, wiping `Moved`).
  Two callers: (1) `p = nullptr` at `VisitBinaryOperator` :2164-2165; (2) `MaybeSetNull` :2580
  at a null-check CFG edge. Both make analyzer-Null coincide with runtime-null:
  * `p = nullptr` literally nulls the runtime value.
  * `MaybeSetNull` nulls p only on the edge where the null-check distinguishes p's OWN nullness
    (`extractDistinguishedTrackablePtr` BSCNullCheckInfo.cpp:137 returns the specific checked ptr).
- **Laundering a Moved (runtime-dangling-nonnull) p to pure-Null via a logical else FAILS** (P5/P6/P7):
  the worklist applies `MaybeSetNull` per-predecessor then **OR-merges** (:2962-2967). `&&` short-
  circuits into 2 condition blocks; each `MaybeSetNull` nulls only its OWN operand; the other
  operand's false-predecessor keeps p=`[Moved]`; the OR-merge gives `[Null,Moved]` at the else block
  → `has(Moved)` fires (`checkCastOPS` :1747 / `checkOPSUse` :926). `-dump-owned-check` confirms
  block-2(else) = `[Null,Moved]` for both `if(p&&q)` and `if(q&&p)`. `||` uses `semanticIntersect`
  (BSCNullCheckInfo.cpp:331) so it never over-nulls. `if(p==q)` (no null literal) is XNOR-rejected
  as a non-null-check (:183).
- **The direct USE path (the literal :927 cell)** needs a cast-to-nonnull or a deref to reach a use
  of a `_Nullable` p; both are independently SHAPE-REJECTED by the nullability checker ("_Nullable
  cannot be dereferenced" / "cannot cast nullable to nonnull"). The only cast-free use (pass to
  `safe_free`'s `_Nullable _Owned` param) routes through `checkCastOPS`, which ALSO carries the
  `Moved` bit on the dangling path. So neither the use-cell nor the cast-cell is reachable with a
  live dangling value.
- **Sound baselines**: P4 `move p; if(p==nullptr){ safe_free(p); }` → ACCEPTED + vg CLEAN (branch
  only runs when p genuinely null). P8 `if((p=take(c))!=nullptr){free}else{free}` → ACCEPTED + vg
  CLEAN (reassignment refills p; both branches sound). P3 control (plain double-free, no null) →
  correctly REJECTED "invalid cast of moved".

**DISTINCT-from-F44/F45 confirmation**: F44/F45 are the WITH-owned-fields S-shape cell
(`SNullOwnedFields` set goes stale / collapses via `&_Mut`). This R2 target is the PLAIN-owned
no-fields cell (`OPSAllOwnedFields[VD].empty()`), a different container and a different guard. It
is a real dead branch but NOT a reachable defect — closed, not folded.

**Status**: NO new filing. Cell marked **CLOSED — unreachable** (E1's call upheld). If a future
change ever lets `setToNull` fire on an edge where the runtime value can be non-null (e.g. a new
`MaybeSetNull` caller, or a logical-combine refactor that drops the OR-merge of Moved), this cell
would immediately become a double-free FN — worth re-probing on any edit to `MaybeSetNull` (:2580),
`NullCheckInfo::operator&=` (BSCNullCheckInfo.cpp:236), or the worklist merge (:2962-2967).

## R4 call/loop move-tracking — VisitCallExpr callee-operand + loop back-edge (2026-05-30)

**Steering**: F88 (init analyzer never checks the Call.Callee operand) explicitly flagged the
ownership analyzer's analogous terminator-callee handling. Audited `VisitCallExpr` (:2207-2227)
+ loop/switch back-edge move tracking for a pure-`_Safe` use-after-move/leak FN.

### VisitCallExpr callee-operand omission (:2207-2227) — STRUCTURAL ANALOG OF F88, but BENIGN
`VisitCallExpr` iterates ONLY `CE->arg_begin()..arg_end()` (:2213) with `op=Move`; it NEVER visits
`CE->getCallee()`. This is the exact structural twin of F88's init gap (`InitAnalysis::run` iterating
only `CD.Args`, skipping `CD.Callee`). **But for ownership it does NOT produce a reachable FN:**
1. **No owned value can sit in the direct callee position** — `_Safe int (*_Owned fp)(int)` is
   SHAPE-REJECTED at the type gate ("type of '...' cannot be qualified by `_Owned`"). A function
   pointer cannot carry `_Owned`, so there is no owned-callee whose move/use needs tracking. (F88's
   init analog used an *uninit* fnptr, not an *owned* one — the uninit state IS reachable for a plain
   fnptr; the moved state is not.)
2. **A move buried in a callee SUBEXPR is caught anyway** — `runOnBlock` (:2604) uses
   `setAllAlwaysAdd` full linearization, so `(get_fn(p))(5)` lowers the nested `get_fn(p)` CallExpr
   into its OWN CFG element; the move of `p` is caught there ("use of moved value: `p`"). (Chain-K
   superset property.) Deref/addr callees (`(*get_holder(p))(9)`) are independently safe-zone-forbidden.

So the callee-operand omission is a real structural asymmetry vs the args, but it has **no reachable
in-scope exploit** in the ownership analyzer. Re-probe only if owned fnptrs ever become legal, or if
the CFG config drops `setAllAlwaysAdd`.

### Loop / switch back-edge move tracking — plain OPS SOUND, struct-field FOLDS into F75
- **Plain `_Owned` pointer (OPSStatus, Moved is a BIT)**: the back-edge / cross-case merge OR's the
  Moved bit, and `checkOPSUse` reads `has(Moved)`. `while(c){consume(p);}` (no reinit) → block-1 entry
  merges to `[Owned,Moved]` and "use of moved value: `p`" fires at the 2nd-iter consume (PLUS a leak
  diag on the 0-iter path). Switch fallthrough + post-switch consume both rejected. **All SOUND.**
  Reinit forms (`consume(p); p=mk();`) correctly ACCEPTED. So plain-OPS move tracking is sound across
  if/switch/loop joins (the F02 family is closed for OPS).
- **Struct field (SOwnedOwnedFields, a SET, no maybe-moved lattice element)**: a field moved inside a
  `while`-loop body and consumed UNCONDITIONALLY after the loop is **ACCEPTED (FN, vg double-free,
  invalid-ops:1)**. `-dump-owned-check` shows the loop-exit merge restores `owned_fields:["p"]` at the
  join despite the body move. → **FOLDED-into-F75.** Same root: `OwnershipImpl::merge` (:254-276) UNIONs
  the owned-field set across predecessors instead of MEETing it; the `while` back-edge is just another
  join reaching the same union. F75's repro used an `if`; the loop is the same bug via a different
  control-flow shape. F75's fix surface (intersect the consumable-owned field set across preds) closes
  this too. NOT a distinct root cause — no new filing. (Probes in /tmp/explorer_probe_loopFieldMerge.*,
  ledger /tmp/probed_R4E2.md.)

**Net**: no new root cause. The VisitCallExpr callee omission is benign (owned-fnptr shape-rejected +
setAllAlwaysAdd superset); the only loop/merge FN folds into F75.

## runOwnershipAnalysis fixpoint + merge OPS-owned-field union across loop back-edge (BSCOwnership.cpp:2966-3017, merge :250-264) — read 2026-06-23 — UNPROBED

**Invariant**: at a CFG join (incl. a loop back-edge), the set of STILL-CONSUMABLE owned
sub-fields must be the MEET (intersection) across predecessors — a sub-field is safely
re-consumable post-join only if owned on ALL predecessors. F75 confirmed this is VIOLATED for
the **S** (struct-with-owned-fields) container: `merge` UNIONs `SOwnedOwnedFields` (:284-286).
The R4 note already folded the while-loop S-field shape into F75.

**The OPS-owned-field container is a DISTINCT code path**: `merge` :250-264 UNIONs
`OPSOwnedOwnedFields` for an `int *_Owned *_Owned p` (owned-pointer whose pointee is itself an
owned pointer = OPS category with owned fields keyed by "*" suffix). Same union shape, DIFFERENT
container + DIFFERENT guard (:255-256 checks `statsA.OPSOwnedOwnedFields.empty()` = WHOLE MAP, like
F75's S guard). The R4 loop probe only covered (a) plain OPS (Moved is a BIT → sound) and
(b) S struct-fields (→ F75). The OPS-nested-owned-pointer cell was NOT probed for the loop
back-edge. checkMemoryLeak (:1958-1963) and checkCastOPS/checkCastBOP read this set.

**Peers**: F75 (S-field union, filed), merge S-branch :276-298, merge BOP-branch :310-320 (3rd
union-shape container), checkMemoryLeak :1958 (reads OPSOwnedOwnedFields), the fixpoint
convergence check :3010 (preVal.equals(val)).

**Candidates**:
1. **OPS-owned-field loop back-edge union** — `int *_Owned *_Owned p`; move inner `*p` in loop body,
   consume inner unconditionally after loop. If merge UNIONs OPSOwnedOwnedFields, the "*" entry is
   restored at the loop-exit join → post-loop consume accepted → double-free FN on the
   iterated path. EXPECTED FOLD-F75 (same union shape) but DISTINCT container/code-path — verify
   whether it's reachable / whether it's caught by the OPSStatus BIT (Moved) the way plain OPS is.
2. **fixpoint reporter double-count / convergence** — runOnBlock (:3008) reports diags on EVERY
   worklist visit, before the convergence check (:3010). A loop body visited N times re-runs transfer.
   Does a leak/move diag fire spuriously multiple times, or get suppressed by checkMemoryLeak's
   reset-to-Moved (:1953-1955)? Lower priority (cosmetic at worst).
3. **BOP-owned-field loop union** (:310-320, 3rd container) — `int *_Owned *_Borrow` nested; but
   BOP outer is _Borrow → IsTrackedType drops it (F34 territory). Likely SHAPE-REJECTED.

## VisitCStyleCastExpr — `(void *_Owned)X` subfield-owned check (BSCOwnership.cpp:2236-2308) — F91 CONFIRMED-new 2026-06-04

**Invariant**: erasing an owned value to `void *_Owned` (the standard libcbs free idiom) must
reject the cast if the value still owns an un-moved `_Owned` subfield (else freeing the void
pointer frees only the outer block and leaks the inner owned pointee).

**Peers** (must all agree on "does this rvalue still own a subfield?"): checkCastOPS / checkCastBOP
(DeclRefExpr path :2246-2257), checkCastField (MemberExpr :2263 and UnaryOperator deref-chain
:2276-2294). The DeclRef path correctly emits err_ownership_cast_subfield_owned.

**Root cause (C3 dispatch-coverage hole)**: the handler dispatches on
`InnerE = subExpr->IgnoreParenCasts()` over exactly {DeclRefExpr, MemberExpr, UnaryOperator(deref)};
the `else` branch (:2297) just `Visit(subExpr)` and runs NO checkCast*. So any owned-bearing rvalue
whose syntactic shape is outside that set (CallExpr, comma/BinaryOperator, ConditionalOperator, …)
erases to void unchecked → inner `_Owned` leaked silently.

**Candidates**:
1. CallExpr / comma inner-expr nested-owned leak — **PROBED-confirmed-F91** (valgrind 4 bytes lost;
   DeclRef baseline rejects the identical value).
2. ConditionalOperator / deeper nesting through the same else branch — UNPROBED (expected FOLD-F91,
   same fix surface: run the subfield check in the else branch / normalize inner expr first).
3. Does checkCastField's deref-count string handle mixed Member+Deref (`(*s).f` chains)? — UNPROBED.

**Distinct from** F64 (outer pointer RAW + IsTrackedType blind; F91 outer is _Owned and the DeclRef
cast IS rejected), F14/F47 (temp not consumed; F91's value IS consumed, the leak is the subfield).

## checkCastOPS / checkCastBOP / checkCastField (BSCOwnership.cpp:1743-1902) — F91 callee family, PROBED-SOUND 2026-06-04

**Invariant**: given a `(void *_Owned)` cast of a var/subfield, ensure the target is (1) not Moved/Uninit
and (2) all its owned sub-fields are already moved (else freeing it leaks the inner owned). Three tracked
categories (BSCOwnership.h:104-108): **OPS** owned-ptr-to-struct, **S** struct, **BOP** basic owned ptr
(`int *_Owned`). DeclRef cast → checkCastOPS/checkCastBOP; deref/member cast → checkCastField.

**Peers**: VisitCStyleCastExpr (:2236, the dispatcher — F91 is its else-branch hole), checkMemoryLeak
(:1904, the end-of-scope leak check — must agree on AllMoved meaning "container still needs freeing").

**Key data**: {OPS,S,BOP}OwnedOwnedFields[VD] = still-owned subfield path-strings. Path separators DIFFER:
OPS/S deeper search = `fullFieldName + "."` (struct members); BOP deeper search = `fullFieldName + "*"`
(deref depth, :1883 "when freeing **, check if *** exists").

**Candidates**:
1. BOP two-level deref-cast state composition (`(void*_Owned)*outer` → AllMoved, then `(void*_Owned)outer`)
   — **PROBED-SOUND**: correct two-level free = 2 allocs/2 frees clean; free-inner-forget-outer correctly
   errors "memory leak of value: `outer`" (AllMoved does NOT suppress container leak). bounds F91 to the
   else-branch only (deref arm with DeclRef base is sound).
2. checkCastOPS (:1747) lacks the `!is(VD, AllMoved)` exception that checkCastBOP (:1781-1782) HAS —
   potential FALSE-POSITIVE (over-strict reject of an AllMoved owned-ptr-to-struct cast). UNPROBED;
   lower priority (false-positive, not soundness) + scope-ambiguous (OPS = struct pointee). Needs `is`/`has`
   bitset semantics to know if AllMoved implies has(Moved).
3. Mixed BOP→struct path: basic owned ptr whose deref reaches a struct-with-owned-field — BOP branch only
   searches `+"*"`, would miss a deeper struct member `"*.f"`. UNPROBED (drifts toward struct scope).

## checkMemoryLeak / canAssign / is / has (BSCOwnership.cpp:1904 / 395 / 321 / 358) — PROBED-SOUND 2026-06-04

**Invariant**: at end-of-scope, every tracked owned var that still owns heap must get a MemoryLeak /
FieldMemoryLeak diag. Driven by `canAssign(VD)` (situation-1 container leak) + non-empty *OwnedOwnedFields
(situation-2 field leak), per category OPS/S/BOP.

**canAssign semantics** (:395): for OPS/BOP, reset {Uninit, Moved, Null} bits then `return !any()` — i.e.
canAssign = true (no leak) ONLY if status ⊆ {Uninit, Moved, Null}; any of {Owned, PartialMoved, AllMoved}
=> false => leak flagged. AllMoved correctly counts as "container still needs free". Status bits
(BSCOwnership.h:93): Uninit=1,Null=2,Owned=4,Moved=8,PartialMoved=0x10,AllMoved=0x20. Bitvectors OR-merged
at joins (mergeStatus :200+) => conservative for leaks (any path owns => flagged).

**is/has quirk** (:321/:358): `is(VD,S)` = S is the ONLY bit set; `has(VD,S)` = S set AND ≥1 other bit.
Reset-and-test idiom; fragile for exact bit-combos but used consistently.

**Peers**: checkCast* (clears OwnedOwnedFields + sets AllMoved — must agree canAssign(AllMoved)=false),
mergeStatus (the OR-merge), init/initOPS/initBOP/initS (:434+ populate the status maps from the type).

**Candidates**:
1. Conditional/loop leak merge soundness — **PROBED-SOUND**: else-path leak, loop-conditional-free leak
   both correctly error "memory leak of value: `p`"; both-paths-freed compiles clean. OR-merge is sound.
2. canAssign has NO SStatus branch (:431 falls to `return false`) — a struct is always "!canAssign". Unused
   by checkMemoryLeak's S path (which checks SOwnedOwnedFields directly), so likely benign; UNPROBED. The
   other canAssign callers (:1088 assign, :1638) on a struct var would always see !canAssign.
3. canAssign treats {Null} as no-leak — a BOP marked Null while still owning heap (stale-null) would be a
   missed leak. Folds toward F44/F45 (stale null field) territory; UNPROBED, likely-fold.

## checkOPSAssign / checkBOPAssign / HandleDREAssign / VisitBinaryOperator-assign (BSCOwnership.cpp:1080 / 1632 / 2431 / 2149) — PROBED-SOUND 2026-06-04

**Invariant**: assigning to an owned var that still owns heap must emit InvalidAssignOfOwned/
PartiallyMoved/AllMoved (the clobbered old value would leak). checkOPSAssign/checkBOPAssign gate on
`!canAssign(VD)` then classify by Owned/PartialMoved/AllMoved; afterward set VD=Owned.

**Dispatch** (VisitBinaryOperator :2150): assign = `op=Move?GetAddr; Visit(RHS)` (consume RHS iff
LHS IsTrackedType) then `op=Assign; Visit(LHS)` (→ HandleDREAssign via VisitDeclRefExpr/MemberExpr/
UnaryOperator/ArraySubscript, all resolving LHS to a base DRE + field-path). Post-overrides (:2164):
`RHS isNullExpr → setToNull(LHS)`; `IsCastFromVoidPointer(RHS) → setToAllMoved(LHS)` — both AFTER the
overwrite check.

**Peers**: checkMemoryLeak (end-of-scope; AllMoved→leak so setToAllMoved stays sound), checkCast*
(consume side), IsTrackedType (F64 — untracked types skip the whole assign check).

**Candidates**:
1. overwrite owned ptr without free, RHS ∈ {plain malloc, nullptr, cast-from-void} — **PROBED-SOUND**:
   all three error "assign to _Owned value: `p`". setToNull/setToAllMoved post-processing does not mask
   the old-value leak (the check runs first). No F91-twin: an assign LHS must be an lvalue
   (DRE/Member/Deref/Subscript — all dispatched), unlike F91's unconstrained cast-operand space.
2. checkBOPFieldAssign (:1664, three-condition owned-field assign) — NOT fully read. UNPROBED; next lead.
3. LHS deref of a non-DRE/Member base (`*func() = x`) falls through VisitUnaryOperator (:2133+) — but the
   overwritten lvalue isn't a tracked VarDecl, so no tracked leak. UNPROBED-likely-low-value.

## checkBOPFieldAssign (BSCOwnership.cpp:1664-1737) — PROBED-SOUND 2026-06-04

**Invariant**: assigning to an owned deref-subfield of a basic owned pointer (`*outer = x`) is valid only
if (1) VD not moved/uninit, (2) the field's parents are not moved, (3) the field + its subfields are all
already moved — else the old owned value at that subfield leaks. Emits InvalidAssignFieldOfMoved /
InvalidAssignFieldOfOwned / InvalidAssignSubFieldOwned. (BOP field-paths are deref strings "*","**",… —
struct-field paths go through checkSFieldAssign/checkOPSFieldAssign instead.)

**Peers**: checkBOPAssign (whole-var assign), checkCastField (the cast-side mirror), HandleDREAssign
(dispatch: `*outer = x` → VisitUnaryOperator → VisitDeclRefExpr(outer,"*") → checkBOPFieldAssign).

**Candidates**:
1. `*outer = i2` overwrite while `*outer` owns i1 (no free) — **PROBED-SOUND**: errors "assign to part of
   _Owned value: `outer*`". Condition-3 overwrite-leak caught.
2. Parent-check loop bound `i > 0` (:1677) excludes the length-1 prefix → for "***" it checks parent "**"
   but skips "*"; assigning `***outer` while only "*" is moved may miss InvalidAssignFieldOfMoved. UNPROBED
   — needs quadruple-nested `int *_Owned`×4 + contrived partial-move; low reachability, likely F64-fold.
   Not chased (would be discovery, not confirmation).
3. findPrefixStrings(BOPOwnedOwnedFields, fullFieldName) at :1704 uses no separator suffix (unlike
   checkCastField's `+"*"`) — prefix "*" would also match "*" itself + "**"; relies on the count() exact
   check at :1696 to handle the field itself. UNPROBED (looks intentional).

**AREA STATUS**: the ownership assign/cast/leak family (checkCast{OPS,BOP,Field}, checkMemoryLeak,
canAssign, check{OPS,BOP}Assign, checkBOPFieldAssign, VisitBinaryOperator-assign) is now thoroughly read
+ probed. One NEW bug (F91, the void-cast else-branch); everything else SOUND. Next cycles: SWITCH analyzer
(nullability narrowing / borrow-checker region inference) to hunt new defect classes.

## owned-field BREADTH tracking (limit-sweep, F96 sibling-dimension) — probing
**Invariant**: a struct with N `_Owned` fields, each assigned-not-freed, must report
ALL N field leaks; the tracked-field set must not have a fixed-size cap (cf. the
SmallPtrSet<_,16>/SmallVector inline sizes in hasOwnedFields/initS).
**Peers**: F96 (depth cap), checkMemoryLeak, SAllOwnedFields.
**Candidates**:
1. **breadth cap (e.g. >16 owned fields) → fields beyond cap untracked → silent leak — probing**.
2. mixed init (assign K of N, free J) breadth interaction. UNPROBED.
3. N owned LOCALS (not fields) breadth. UNPROBED.

## use-after-move wrapper-strip (HandleDREUse / move-tracking) — C1 wrapper probe
**Invariant**: using a moved-out `_Owned` value must be rejected regardless of a
paren/comma/cast wrapper around the use (the C1 class: F12/F14/F46/F91).
**Peers**: checkMoveVar/HandleDREUse, F91 (comma-wrapped cast skips subfield check),
F14 (paren CallExpr), F46 (`(s).b` paren).
**Candidates**:
1. **comma/paren-wrapped use of a moved owned not caught — probing**. If the
   use-after-move check keys on a bare DeclRef, `q = (0, p)` after `move(p)` slips → double-free.
2. cast-wrapped use `(int*_Owned)p` after move. UNPROBED.
3. use-after-move inside a ternary arm. UNPROBED.

## branch-divergent owned free (OwnershipImpl::merge MEET) — probing (F75 area)
**Invariant**: an owned freed on SOME paths but not all must flag a leak on the
not-freed path (merge = MEET: owned only if owned on ALL preds). F75 = merge UNIONs.
**Peers**: F75 (merge union-not-meet), F22 (if-cond temp leak), checkMemoryLeak.
**Candidates**:
1. **free p in if-branch only → else-path leak caught? — probing** (FOLD-F75 if missed).
2. free p in if + unconditional free after → double-free on if-path. UNPROBED.
3. free in BOTH branches (no leak) → control, expect ACCEPT.

## loop-body owned consume (back-edge moved-state) — probing (scalar, F75-loop area)
**Invariant**: an owned consumed in a loop body must be flagged use-of-moved on the
next iteration (the back-edge carries the moved state into the loop head).
**Peers**: F75 (struct-field merge union at joins incl back-edges), scalar-merge SOUND (cycle 20).
**Candidates**:
1. **free scalar p in loop body → iteration-2 double-free caught? — probing** (scalar; expect SOUND).
2. struct-field free in loop → FOLD-F75 (union-not-meet at back-edge).
3. owned re-init each iteration (free + re-malloc) → control, expect ACCEPT.

## ownership state across _Unsafe block boundary — probing
**Invariant**: an ownership state change (free/move) inside an `_Unsafe { }` block
inside a `_Safe` fn must be reflected in the `_Safe` analysis after the block (or
the boundary handled conservatively), else a use-after-free in _Safe is missed.
**Peers**: F95 (storage-context skip), safe-zone gating, lowerStmt SafeStmt (:262).
**Candidates**:
1. **free p in _Unsafe block, use p in _Safe after → caught? — probing**.
2. move p in _Unsafe, leak-check at _Safe scope exit. UNPROBED.
3. owned declared in _Unsafe block used in _Safe. UNPROBED.

## ownership-analysis complexity vs owned-local count (timing-sweep, deployed oracle) — probing
**Invariant**: ownership tracking time should be ~linear in the number of owned
locals/moves; super-linear = compile-time DoS (cf. borrow checker O(N²)).
**Peers**: analyzer-complexity-blowup-timing oracle (found O(N²) borrow fixpoint), checkMemoryLeak.
**Candidates**:
1. **N owned locals (alloc+free each) → super-linear ownership time? — sweeping**.
2. N moves of one owned through a chain. UNPROBED.
3. N nested scopes with owned. UNPROBED.

## move-tracking breadth (boundary-sweep, deployed oracle) — probing
**Invariant**: in a chain of N moves (q0=p; q1=q0; …), using an early-moved variable
must always be flagged use-of-moved — no fixed cap on the tracked moved-set.
**Peers**: owned-field breadth (sound, cycle 13), boundary oracle (F96).
**Candidates**:
1. **N-move chain, then use q0 (moved at step 1) → caught at all N? — sweeping**.
2. N moved-then-reinit vars. UNPROBED.
3. interleaved move/use breadth. UNPROBED.

## SafeExpr-peel coverage across move/cast sites (C1, F65/F46 sibling) — probing
**Invariant**: a `_Safe(expr)` (SafeExpr) wrapper must NOT defeat ownership
move-tracking / cast checks. F65 added IgnoreParen*Safe peel at the nullability-
narrowing site, but ~20 BSC sites still use plain IgnoreParenImpCasts (no SafeExpr
peel) → a sibling may let SafeExpr launder a soundness check.
**Peers**: F65 (SafeExpr defeats null-narrowing, fixed), F46 (paren member move), F91 (void-cast else).
**Candidates**:
1. **use-after-move through `_Safe(p)` source → move not tracked → FN? — probing**.
2. void-owned cast of `_Safe(p)` → FOLD-F91 (else-branch). 
3. SafeExpr in a borrow source. UNPROBED.

## owned branch-divergent through SWITCH (switch-join merge) — probing
**Invariant**: an owned freed in some switch cases but not all must flag a leak on
the unfreed path (switch-join = MEET, like the if-join, cycle 20 sound).
**Peers**: scalar if-merge (sound, cycle 20), F90 (switch-discriminant uninit), F75 (struct-field merge union).
**Candidates**:
1. **free p in case 1 only, default leaks → caught? — probing**.

## owned value through ternary (ConditionalOperator merge) — probing
**Invariant**: an owned from `c ? mk() : mk()` (both arms owned) must be tracked;
not freeing the result must be caught as a leak (ternary arms merge to owned).
**Peers**: F25 (ConditionalOperator in CheckMoveVarMemoryLeak), if/switch merge (sound).
**Candidates**:
1. **owned from ternary, not freed → leak caught? — probing**.
2. owned from ternary, freed → ACCEPT (control).

## VisitAbstractConditionalOperator (:2224) self-referencing ternary RHS of owned assign — UNPROBED 2026-06-23

**Invariant**: for `q = c ? q : mk()` (q already _Owned), the analyzer must model that
on the true path q is self-assigned (no leak), on the false path q's old allocation is
overwritten (leak unless old freed). The merge of arm states must NOT let a real
overwrite-leak (false path) or a real double-free escape.

**Mechanism (source-read)**: VisitBinaryOperator (:2186) for assign does
`op=Move; Visit(RHS)` THEN `op=Assign; Visit(LHS)→checkOPSAssign`. For ternary RHS,
VisitAbstractConditionalOperator (:2224) visits true-arm `q` with op=Move (marks q Moved),
resets `stat=StatAfterCond`, visits false-arm `mk()` (q untouched → still Owned),
then `merge(StatAfterTrue=Moved, stat=Owned)` → multi-bit Owned|Moved. Then checkOPSAssign
(:1112) tests `has(VD,Owned)||is(VD,Owned)` → true → fires InvalidAssignOfOwned.
QUESTION: does the self-move (true arm marking q Moved) SUPPRESS the false-arm overwrite-leak
that SHOULD fire? i.e. does the Moved bit from the unrelated true path make checkOPSAssign
think q was already consumed (canAssign true) and skip the leak diag on the false path?

**Peers**: F75 (merge UNIONs owned-field set), checkOPSAssign (:1105), F25 (Ck MoveVar ternary).

**Candidates**:
1. **`q = c ? q : mk()` (q already owned) — false path overwrite-leak suppressed by true-arm self-move? — PROBING**.
2. `q = c ? mk() : q` (arms swapped) — symmetry check.
3. nested `q = c ? (d ? q : mk()) : mk()` — deeper merge.

## safe_swap of owned values (runtime ownership preservation) — probing
**Invariant**: `safe_swap(&_Mut a, &_Mut b)` for owned a,b swaps the allocations;
each still owns one; freeing both after is clean (no leak / no double-free).
**Candidates**: 1. swap two owned, free both → valgrind clean?

## borrow an _Owned then free after borrow ends — probing
**Invariant**: `&_Mut p` (p is _Owned) borrows the owned pointer WITHOUT moving it;
after the borrow's last use, p is still owned; freeing it is clean (1 alloc/1 free).
**Candidates**: 1. borrow owned p, use borrow, free p → valgrind clean?

## owned through goto / label fall-through — probing
**Invariant**: an owned freed then re-freed via a goto/fall-through path must be
caught (the CFG must track ownership through goto/label edges).
**Peers**: scalar if/switch/loop merge (sound), F90 (goto in init = SwitchInt? no).
**Candidates**: 1. `if(c)goto skip; free p; skip: free p;` (else-path double-free) → caught?

## const-qualified _Owned pointer — probing
**Invariant**: `const int *_Owned p` (const pointee) must still track ownership; alloc+free
balanced, leak caught. const shouldn't defeat ownership tracking.
**Candidates**: 1. const-owned alloc + free → balanced? 2. const-owned not freed → leak caught?

## VisitBinaryOperator (:2149) + VisitAbstractConditionalOperator (:2187) — read, SOUND (F90 area)
Assignment: move is DESTINATION-based — `op = IsTrackedType(LHSType) ? Move : GetAddr` (RHS consumed only
when LHS is move-semantic/owned; handles owned→_Bool no-consume). RHS null → setToNull(LHS); cast-from-void
(safe_malloc) → setToAllMoved(LHS). Non-assign ops: comparison/logical/additive → GetAddr (operands not
consumed); comma LHS always GetAddr (asymmetric). The comma/logical owned-temp-not-consumed is the F90 area
(filed). Ternary: visit both branches op=Inherited, merge(true, false) — conditional-move handled (probed sound).
No new gap.

## checkOPSDerefAssign (:1115) — candidates 2026-06-17 (was unread)
INVARIANT: `*p = v` (p owned ptr): if p moved → InvalidUseOfMoved; if pointee move-semantic and *p already Owned → InvalidAssignOfOwned (overwrite-leak); else mark *p owned. Non-move-semantic pointee (int) → plain value write, no leak.
Candidates:
1. [overwrite-leak] `*pp = new` where pp:`int*_Owned*_Owned` and *pp already owns → InvalidAssignOfOwned; FN if old leaked silently. **UNPROBED** (top)
2. [moved] write `*p=v` after p moved → InvalidUseOfMoved; verify. UNPROBED
3. [partial] *p where p PartialMoved → InvalidAssignOfPartiallyMoved. UNPROBED

## VisitArraySubscriptExpr (:2044) — candidates 2026-06-17 (was unread)
INVARIANT: `a[i]`→path `a`+`*`-suffix (one `*` per subscript level); index `i` NOT visited here (separate CFG element via setAllAlwaysAdd, so OK). ALL elements collapse to ONE path `arr*` (no index distinction).
Candidates:
1. [aliasing-imprecision] owned array `T*_Owned arr[N]`: move arr[0] then op on arr[1] — both = `arr*` → FP (arr[1] seen moved) OR FN (per-element leak missed). **UNPROBED** (top)
2. [nested] `arr[i][j]` double-suffix path mapping correctness. UNPROBED
3. [member-array] `s.arr[i]` memberField+suffix path. UNPROBED

## VisitCallExpr (:2207) — candidates 2026-06-17 (was partial)
INVARIANT: each call arg → op=Move (consume) unless arg type isBooleanType (GetAddr, no consume — rule13/F101); void-cast arg w/ owned fields → PassCastToArgOrRet diag.
Candidates:
1. [double-move] `f(p, p)` (owned p twice) → 1st moves, 2nd = use-after-move; FN if 2nd not caught. **UNPROBED** (top)
2. [comma] `f((g(), p))` move through comma. UNPROBED
3. [bool] arg-type-bool uses ARG type not PARAM — if conversion-to-bool elided, owned consumed wrongly? UNPROBED (F101 gates owned→bool)

## VisitMemberExpr (:2068) — candidates 2026-06-17
INVARIANT: member assign/use routed to HandleDREAssign/Use(DRE, fieldpath) ONLY when getMemberFullField(ME).first is a DeclRefExpr; non-DRE base (call-result/complex) → no DRE → tracking dropped (C3). In the F44/F45/F61/F67/F99 struct-field cluster.
Candidates:
1. [C3 non-DRE base] `mk().ownedfield` / `(*p).ownedfield` move — tracked? FN if base non-DRE drops it (likely folds to field-cluster). **UNPROBED** (top)
2. [deref-base] move nested owned via `(*p).q` (p owned ptr to struct w/ owned field). UNPROBED

## isAddrMut stale-instance-flag leak (TransferFunctions::VisitUnaryOperator :2138-2139 sets, HandleDREUse :2519/2546/2569 resets) — CONFIRMED-new 2026-06-18

**Invariant**: `isAddrMut` (TransferFunctions instance field, :2000) must reflect ONLY whether
the CURRENTLY-processed `&_Mut <expr>` is mutably borrowing the var about to be use-checked. It
is set true at VisitUnaryOperator :2138-2139 (UO_AddrMut) and consumed by checkOPSUse/checkSUse/
checkBOPUse's Null→Owned collapse (the F44 migration, checkSUse :1296-1302).

**Root cause (stale-state-not-reset)**: `isAddrMut` is reset to false ONLY in HandleDREUse's
three `fullFieldName==""` whole-var branches (:2519, :2546, :2569). It is NOT reset when:
(a) the `&_Mut` operand DRE resolves to a VarDecl that is in NONE of OPSStatus/SStatus/BOPStatus
(an UNTRACKED var, e.g. a plain `int x`); (b) the DRE is a field-access form (the `else` branches);
(c) the DRE isn't a VarDecl at all. The `TransferFunctions` object lives for the WHOLE CFG block
(runOnBlock :2607 constructs ONE TF, loops all elements :2609), so a stale `true` leaks forward —
across subexpressions AND across separate statements in the same block — into the NEXT tracked
struct use, which then wrongly fires the F44 Null→Owned collapse → spurious `field memory leak`.

**Asymmetry PROVEN (3 ways)**:
- `mt(&_Mut x); cs(&_Const s);` (x = untracked int, s = struct w/ null-init `_Owned _Nullable`
  field, ONLY `&_Const`-borrowed) → `field memory leak of value: s, s.b is leak` (FP).
  Minimal repro /tmp/explorer_probe3.cX4B5I.cbs.
- BASELINE one-token diff `mt(&_Const x); cs(&_Const s);` → CLEAN. /tmp/explorer_baseline3.fQX0dc.cbs.
- ORDER CONTROL `taker2(&_Const s, &_Mut x)` (struct used FIRST, before the flag is set) → CLEAN.
  /tmp/explorer_probe2.lhqmvC.cbs. Proves order-dependent stale state, not independent misclassify.

**DISTINCT from F44/F45**: F44 = `&_Mut s` applied DIRECTLY to the struct (the collapse logic is
correct-but-too-aggressive). HERE the struct is NEVER mutably borrowed — the `&_Mut` is on an
UNRELATED untracked var; the leaked flag is the root cause, not the collapse. F45's `&_Const s`
trigger (2026-05-22 note) is a different mechanism (stale SNull from a prior move → runtime
double-free); this needs NO move, NO prior reassign, and is a pure FP. The fix surface is also
distinct: F44's fix changes the collapse at :1296-1302; this needs `isAddrMut = false` at the
START of VisitUnaryOperator/HandleDREUse (or reset on every non-matching path), independent of F44.

**Defect class**: C5-adjacent but really a NEW shape — "instance-scoped flag not reset across
visited elements" (a stale-state leak in the dataflow visitor). Severity MEDIUM (false positive;
rejects valid `_Safe` code; the spurious leak forces dead defensive frees on an unrelated struct).

**Blast radius**: any block where a `&_Mut` of an untracked/field/non-VarDecl operand is followed
by a tracked struct (or OPS/BOP) use — the sibling checkOPSUse/checkBOPUse isAddrMut blocks would
collapse their own Null state the same way (out-of-scope feature combos, parallel fix surface).

## isAddrMut lifecycle leak (stale flag across CFG block) — FOLDED into F44 (2026-06-22)
- `&_Mut` on an UNtracked/field/non-VarDecl operand sets isAddrMut (:2139) but no reset site (:2519/2546/2569) fires; the stale `true` persists in the per-block TransferFunctions (:2607) into a later unrelated tracked-struct use, where checkSUse:1296-1302 (= F44 root) collapses SNull→SOwned → spurious "field memory leak". SAME fix surface as F44 (remove the migration). NOT a separate filing; widens F44 blast radius (fires without `&_Mut` on the struct itself). Found by GLM-5.2 exploiter, folded by Conductor.

## TransferFunctions::VisitCStyleCastExpr — MemberExpr base != DeclRefExpr skip (BSCOwnership.cpp:2300-2308) — PROBED-folded-into-F30 (INCOMPLETE-FIX survivor, 2026-06-23)

**VERDICT (2026-06-23)**: candidate 1 `(void*_Owned)(*p).f` (single-level `int*_Owned f`,
p=`_Borrow` of local struct) → **COMPILES CLEAN (FN) + valgrind double-free (1 alloc/2 frees,
Invalid free())** when followed by a tracked `(void*_Owned)s.f` second free. Asymmetry baseline
(both frees via DeclRef-member `s.f`) → REJECTED "invalid cast ... of moved or uninitialized value:
`s.f`". ROOT = getMemberFullField (:115-131) returns a UnaryOperator(deref) base for `(*p).f` →
`dyn_cast<DeclRefExpr>(memberField.first)` fails → checkCastField skipped → move untracked.
This **FOLDS into F30** (getMemberFullField wrapper-list incomplete; same function/invariant).
HOWEVER F30 was marked FIXED by 95c83b5 which added ONLY the ParenExpr arm — the UnaryDeref base
(`(*p).f` = `p->f`) is NOT covered and STILL reproduces post-fix. So this is an INCOMPLETE-FIX
survivor (the double-free symptom is more severe than F30's originally-noted FP). NOT a new root
cause per discipline (same function). Flag to main thread: extend F30's fix to rebase UnaryDeref-of-
pointer bases onto the pointer DeclRef. Candidate 3 `mkS().f` SHAPE-REJECTED (whole-struct-temp leak
check F14-family fires first). The UnaryOperator-branch `*s.pp` variant (:2313, its own walker) is a
distinct code path but yields only a leak-FP (scope-end net masks), not an FN.

**Invariant**: `(void *_Owned)X` where X canonicalizes to a MemberExpr `base.f`/`base->f` must run
`checkCastField` (the err_ownership_cast_subfield_owned + move-of-moved guard) on the field's tracked
owner. This is a DIFFERENT skip site than F91's top-level `else` (:2334).

**The gap**: the MemberExpr branch (:2300) calls `getMemberFullField(ME)` → `{base, fieldName}`,
then proceeds ONLY `if (DRE = dyn_cast<DeclRefExpr>(memberField.first))` (:2302). `getMemberFullField`
walks the chain stripping ParenImpCasts, so `base` ends up = whatever the innermost non-MemberExpr base
is. If that base is NOT a DeclRefExpr — e.g. `(*p).f` (base = UnaryOperator deref), `arr[i].f`
(base = ArraySubscriptExpr), `mk().f` (base = CallExpr) — the dyn_cast fails and the WHOLE check is
skipped with NO else-fallback Visit. The branch is entered (so F91's else never runs) but nothing checks
the subfield-owned state → an _Owned subfield silently dropped (leak) or moved-then-reused (UAF).

**Peers**: F91 (top-level else :2334), F46 (FIXED — getMemberFullField now peels ParenExpr but the
RESULT can still be a non-DeclRef base), checkCastField (:1847), getMemberFullField (:115).

**Candidates**:
1. `(void *_Owned)(*p).f` where `struct S{int *_Owned *_Owned f;}` and `S *p` — base after strip is
   UnaryOperator(*p), not DeclRef → checkCastField skipped → inner owned leaked. The byte-identical
   `s.f` form (s a local) IS rejected → asymmetry. HIGHEST reachability + in-scope.
2. `(void *_Owned)arr[0].f` (array of structs) — base = ArraySubscriptExpr → skipped.
3. `(void *_Owned)mk().f` (struct-returning call) — base = CallExpr → skipped. (overlaps F91 CallExpr
   but distinct because the MEMBER branch is entered, not the else.)

**Distinct from F91**: F91's else (:2334) runs `Visit(subExpr)` and the value isn't a MemberExpr.
Here InnerE IS a MemberExpr so :2300 is entered; the skip is the inner DeclRef-extraction failing with
no fallback. Different code path, different fix (the MemberExpr branch needs to handle non-DeclRef bases).

## findMovedFieldKey (BSCOwnership.cpp:89-103) — PROBED-SOUND 2026-06-23 (the F67/F99 fix machinery, bin 34e6f26e / src 18111bd2)

**Invariant**: given a deref field-key (e.g. `pp**`/`pp*`), walk back through trailing `*`
markers (`pp**`→`pp*`→`pp`) and return the OUTERMOST key that is in `allFields` but NOT in
`ownedFields` and (S-path only) NOT in `nullFields` — i.e. the field that was actually moved
out. Used to report a moved-read at the correct nesting level.

**Peers**: `checkSFieldUse` (:1372 — passes `&SNullOwnedFields[VD]`), `checkOPSFieldUse`
(:1040 — passes `nullptr`, no OPS null-set exists). Both also share the prefix-set
partial-move check (`allPrefixStrs.size() != ownedPrefixStrs.size()`).

**Candidates probed (all SOUND/FOLD):**
1. (FN) nested owned-pointer FIELD `int *_Owned *_Owned pp`, move inner `consume(*s.pp)`
   then re-read `**s.pp` — REJECTED "use of moved value: `s.pp*`". Double inner-move also
   REJECTED. Move outer `consume(s.pp)` then `**s.pp` — REJECTED "use of moved value:
   `s.pp`". OPS variant (`struct *_Owned ps`, `*ps->pp`) — REJECTED "use of moved value:
   `*ps.pp`". The `*`-walk matches the stored keys in both directions. SOUND.
2. (carried-state FN) move inner `pp*`, then move WHOLE outer `s.pp` into a fresh local —
   REJECTED "use of partially moved value: `s.pp`, *s.pp is moved" (conservative; the inner-
   moved state correctly blocks the whole-move). SOUND.
3. (FP) read whole outer `s.pp` after inner moved (pass to free_outer) — REJECTED
   "use of partially moved value" + the callee's `(void*_Owned)q` cast REJECTED "not all
   moved value". Conservative-but-sound (prevents inner leak via the moved-out outer).
4. (OPS-null FP, the `nullptr` asymmetry) — couldn't construct: assigning `nullptr` to a
   nested OPS field is gated ("assign to part of _Owned value") unless freed first, and the
   `_Nullable` qualifier needed for null then blocks the `safe_free` cast. The OPS-null FP
   surface is not reachable from in-scope BSC for the nested-pointer shape.

**setToNull(Expr*) MemberExpr S-path star-suffix gap (:848-859)** — `findPrefixStrings(
SAllOwnedFields[VD], memberField.second + ".")` only collects DOT-prefixed children; the
`f*` asterisk-suffix nested-deref keys are NOT erased from `SOwnedOwnedFields` nor inserted
into `SNullOwnedFields` on `s.f = nullptr`. This is the SAME dot-vs-star prefix-set mismatch
as **F23** (HandleInitListExpr null-init path), class C5 — just a different call site
(assignment vs init-list). **FOLD-into-F23**, not a distinct root cause. (Probe shape blocked
by `_Nullable`-cast tension anyway.)

**NET**: the nested deref-field move machinery (findMovedFieldKey + the S/OPS FieldUse
prefix-set checks) is SOUND for all in-scope nested-owned-pointer-field shapes at 18111bd2.
F67 (deref-of-moved-field) and F99 (deref-store init) are genuinely fixed and not
re-findable. F64 (LOCAL `int *_Owned *pp`, not a field) remains the only open deref FN and
is a DIFFERENT surface (no struct field). No new root cause on this surface.

## checkBOPFieldUse (BSCOwnership.cpp:1591+) — BOP = "basic owned pointer" field-use, the 3rd FieldUse leg
INVARIANT: must flag use of a moved/uninit field-projection of a basic-owned-pointer local, incl.
deref-marked (`*`) paths, consistent with checkSFieldUse (S) and checkOPSFieldUse (OPS).
PEERS: checkSFieldUse (has SNullOwnedFields + findMovedFieldKey), checkOPSFieldUse (findMovedFieldKey,
nullptr null-set). checkBOPFieldUse is the ODD one: a MANUAL substring-prefix loop
(`fullFieldName.substr(0,i+1)`), NO findMovedFieldKey, NO null-set.
CANDIDATES:
1. [symmetry, UNPROBED] BOP uses the manual prefix walk, NOT the findMovedFieldKey deref-marker
   normalization the F67 fix (f33e9f77/7d492a83) added to S/OPS → a deref-of-moved on a BOP
   field-projection (`*bop.f` after move) may slip = FN. RISK: may fold into F64 (local `*pp` deref).
2. [reachability, UNPROBED] no null-set (vs S's SNullOwnedFields) → a nulled BOP field's use
   classification may diverge from S → FP/FN.
3. [cosmetic, SHAPE-REJECTED] diag message order reversed (`fullFieldName + VD->getNameAsString()`
   vs the S/OPS `VD name + "." + fullFieldName`) → wrong message text only, LOW, not soundness.
TOP candidate = #1; defer probe to a GLM explorer (assign focus: checkBOPFieldUse deref-of-moved),
dedup-gate vs F64 first.

### 2026-06-23 — candidate #1 CONFIRMED-new (bin 34e6f26e) — BOP-local deref-of-moved slips; NOT F64, NOT F67

**Repro**: `/tmp/explorer_probe_bop_nested.cbs`. `int *_Owned *_Owned bop` (BOTH levels _Owned → outer
IS owned → IsTrackedType true → bop enters BOPStatus, tracked). `BOPAllOwnedFields[bop] = {"*","**"}`.
`consume(*bop)` moves the outer owned field `"*"`, then `int x = **bop;` (field `"**"`) → compiles CLEAN
(exit 0); valgrind `Invalid read of size 4` (2 allocs/2 frees/12 bytes). Runtime: `*bop` was freed by
consume, then `**bop` derefs through the freed outer → use-after-free read.

**Root cause**: the manual prefix-walk at `checkBOPFieldUse` :1597-1606
  `for (int i = fullFieldName.length() - 2; i > 0; i--) { current = fullFieldName.substr(0, i+1); ... }`
For a 2-char field name (`"**"`, len 2), `i` starts at `len-2 = 0` and the guard `i > 0` is FALSE →
the loop body NEVER runs → the moved OUTER field `"*"` (the parent owned deref) is never consulted.
The literal `BOPAllOwnedFields.count("**")` check at :1627 only fires if `"**"` ITSELF was erased; here
only `"*"` was erased, so the moved-parent read slips. `checkSFieldUse`/`checkOPSFieldUse` avoid this
via `findMovedFieldKey` (BSCOwnership.cpp:89-103, added by the F67 fix 7d492a83) which walks trailing
`*` markers via `pop_back()` until empty — checking EVERY level incl. the outermost — with no `i>0`
boundary. The F67 fix (f33e9f77/7d492a83) added `findMovedFieldKey` to checkSFieldUse (:1372) and
checkOPSFieldUse (:1040) but **NOT to checkBOPFieldUse** — the BOP leg was missed.

**Asymmetry (3-way, airtight on bin 34e6f26e)**:
- BASELINE `/tmp/explorer_baseline_bop_double_consume.cbs`: `consume(*bop); consume(*bop);` →
  `error: use of moved value: *bop` (move IS tracked → field `"*"` erased). Proves the gap is ONLY
  at the deref-site check, not move-tracking.
- BASELINE `/tmp/explorer_baseline_bop_local.cbs`: local `int *_Owned p; consume(p); *p;` →
  `error: use of moved value: p` (local single-level BOP deref-of-moved IS caught via HandleDREUse "" path).
- BASELINE `/tmp/explorer_baseline_s_family.cbs`: struct-field `struct DoublePtr q; sink_pp(q.pp);
  **q.pp;` → `error: use of moved value: q.pp` (S-family findMovedFieldKey catches it — the F67 fix).
  IDENTICAL construct to PROBE-1 but routed through checkSFieldUse → caught. This is the smoking-gun
  asymmetry: S catches, BOP-local slips.

**Control (boundary confirmation)**: `/tmp/explorer_probe_bop_triple.cbs` triple-nested
`int *_Owned *_Owned *_Owned bop`, move `**bop` (field `"**"`), deref `***bop` (field `"***"`, len 3) →
`error: use of moved value: ***bop` CAUGHT. The 3-char name lets the manual walk run ONE iteration
(`i=1`, checks `"**"`). So the slip is specifically at the 2-char-name boundary (`i=0`, guard fails).

**Dedup gates**:
- NOT F64 (umbrella): F64 is `int *_Owned *pp` (outer RAW/unowned → IsTrackedType false → pp NEVER
  enters BOPStatus, move untracked entirely). Here outer IS `_Owned` → tracked; double-consume
  baseline proves the move is recorded. Gap is the deref-site parent-walk, not IsTrackedType.
- NOT F67 (umbrella, now FIXED on this bin per .fresh_verdict.tsv): F67 is the S-family gap in
  checkSFieldUse (lacked the prefix-walk entirely); its fix (f33e9f77/7d492a83) added findMovedFieldKey
  to checkSFieldUse + checkOPSFieldUse ONLY. checkBOPFieldUse was NOT touched and still has the buggy
  manual walk. Distinct function, distinct code site, distinct fix surface (the F67 fix needs extending
  to the BOP leg — same defect CLASS C1/C5 sibling-predicate asymmetry, but a distinct un-fixed site).
- NOT F91 (umbrella): F91 is `(void*_Owned)X` cast skipping subfield check in VisitCStyleCastExpr
  (inner-expr shape gate), manifesting as a LEAK. Here: deref-site in checkBOPFieldUse, manifesting as
  Invalid-read use-after-free. Different function, different symptom.

**Defect class**: C1 (sibling-predicate asymmetry — the F67 fix normalized 2 of 3 FieldUse legs, missed
BOP) / C5 (the manual `substr(0,i+1)` walk's `i>0` boundary under-approximates the moved-parent set,
analogous shape to F26/F44 merge under-approximation). **Severity**: HIGH (silent use-after-free read;
compile-clean → runtime Invalid read; construct is standard BSC `_Owned` nested pointer, in scope).

**Fix surface**: replace the manual `for (i=len-2; i>0; i--)` prefix-walk at :1597-1606 with a call to
`findMovedFieldKey(BOPAllOwnedFields[VD], BOPOwnedOwnedFields[VD], /*nullFields=*/nullptr, fullFieldName)`
— the exact machinery the F67 fix added for S/OPS. Sibling fix to F67 (extend the normalization to the
BOP leg). Also consider adding the `*`→`.` double-call in HandleDREUse's BOP branch (:2608-2612) to
mirror OPS (:2563-2571) / S (:2586-2594), though the findMovedFieldKey fix alone suffices for this case.

## bare-expression-statement use-after-move READ (runOnBlock 5-kind filter, BSCOwnership.cpp:2652-2657) — UNPROBED
**Invariant**: any READ of a Moved/Owned local in `_Safe` (incl. a bare deref `*p;` / cast `(void)*p;`
/ comparison `p==q;` expression statement) must be use-checked — `HandleDREUse` must run so a
use-after-move READ is diagnosed.
**Peers**: the init-analysis `InitAnalysis::run` (which DOES use-check bare deref statements — F83/F88
family shows it reads operands of every terminator + transferStatement) and the borrow checker's
`DefUse` (which visits `UnaryDeref`). The OWNERSHIP analyzer's `runOnBlock` is the odd one: a 5-kind
allowlist (`DeclStmt | CallExpr | assign-BO | inc/dec-UO | ReturnStmt`) that excludes bare
`UnaryOperator(UO_Deref)`, bare non-assign `BinaryOperator` (compare/comma-as-stmt), and bare
`CStyleCastExpr` statements.
**Candidates**:
1. [reachability, UNPROBED] `int *_Owned p = mk(); safe_free((void*_Owned)p); *p;` — the bare `*p;`
   statement (UO_Deref, NOT inc/dec) is skipped by the :2652-2657 filter → `HandleDREUse` never runs →
   the use-after-free/move READ is ACCEPTED in `_Safe`. Baseline `int x = *p;` (same deref, bound to a
   local = a DeclStmt) IS diagnosed "use of moved value: p". This is the GLM finding (probed
   2026-06-23, rc=0 FN) — confirm it is NOT yet filed and probe the runtime (valgrind Invalid read).
2. [symmetry, UNPROBED] `(void)*p;` after free — `CStyleCastExpr` statement also excluded from the
   5-kind filter → same FN via a different excluded kind. Tests breadth of the filter (one excluded
   kind vs all).
3. [composition, UNPROBED] bare comparison `p == q;` after `p` moved — non-assign `BinaryOperator`
   statement excluded → the moved-pointer READ in the comparison slips. Lower severity (read of
   pointer bits, no UB unless the freed memory is accessed) but same root.
TOP candidate = #1 (valgrind-confirmed use-after-free READ = HIGH). Dedup-gate: F64 (`consume(*pp)`
local-deref leak — DIFFERENT: F64 is a CALL-arg move of `*pp` that leaks the inner owned; this is a
bare-statement deref-READ of an already-moved pointer, root = runOnBlock filter, different function
runOnBlock:2652 vs the call-arg path). F98 (brace-less switch init-blind — different analyzer
BSCIRInitAnalysis, different mechanism). Verify distinct before filing.

## switch-fallthrough owned merge (OwnershipImpl::merge × CFG case-entry) — probe 2026-06-24
**Invariant**: at a switch `case` label reachable BOTH by fall-through (predecessor where `p` is
Moved) AND directly from the switch head (`p` Owned), the merged state must be ≤ Owned (i.e.
Moved or MaybeMoved) so a consume/use in that case is flagged on the fall-through path.
**Peers**: OwnershipImpl::merge (:209-309), runOnBlock visit-filter (F111), VisitCallExpr (consume=move).
**Candidates**: 1. **merge(Moved,Owned)=Owned (optimistic) → fall-through double-consume accepted →
runtime double-free** UNPROBED ⭐. 2. case-1 no-break leak-on-direct-path. 3. default vs explicit-case asymmetry.

## HandleDREAssign (BSCOwnership.cpp:2468-2502+) — read 2026-06-24
**Invariant**: dispatches an assignment-LHS DeclRefExpr across 6 situations (OPS whole/deref/field,
S whole/field, BOP whole/field), mutating per-VD owned-field state. The RHS-evaluation move is handled
by the caller's `op=Move` Visit of the RHS.
**Peers**: VisitBinAssign, checkOPSFieldAssign (dual-called at :2491-2500 for `*`-trailing names = the
OPS-only C5 candidate, OOS owned-struct), checkBOPAssign, setToAllMoved.
**Candidates**: 1. **chained owned assign `a = b = mk()` (BOP, in scope)**: does the inner-assign result
move `b` into `a` (b→Moved, a→Owned)? Runtime: 1 alloc/1 free or double-free/leak if mis-tracked. UNPROBED ⭐.
2. OPS `*`-trailing dual-call double-state-mutation (OOS owned-struct). 3. self-assign `a = a`.

## array-of-owned element tracking (VisitInitListExpr :2433 + checkSUse array) — probe 2026-06-24
**Invariant**: `int *_Owned arr[N] = {mk(),...}` — each element owns its value; at scope exit every
un-moved element must be freed exactly once; moving arr[i] marks only element i moved.
**Peers**: VisitInitListExpr (each init op=Move), checkMemoryLeak, init-analysis array guard (:236 arrays
not marked by element write).
**Candidates**: 1. **per-element owned tracking: free arr[0] & arr[1] → 2 alloc/2 free (sound) vs
double-free/leak (mis-tracked)** UNPROBED ⭐. 2. free only arr[0] → leak arr[1] flagged? 3. arr[i] with runtime i.

## _Safe/_Unsafe block boundary × ownership move-tracking — probe 2026-06-24
**Invariant**: the ownership dataflow runs on the whole-function CFG; a move inside an `_Unsafe {}` block
in a `_Safe` fn must be tracked when control returns to `_Safe` (safe-zone is a Sema concept, not a
dataflow boundary).
**Peers**: IsInSafeZone (Sema), runOnBlock, VisitCallExpr move.
**Candidates**: 1. **move in `_Unsafe {consume(p);}` → no leak FP + use-after-p flagged (sound) vs
move lost across boundary (leak FP or UAM FN)** UNPROBED ⭐. 2. alloc inside _Unsafe, used in _Safe. 3. partial move.

## owned return via function-pointer (indirect) call — probe 2026-06-24
**Invariant**: `int *_Owned p = fp()` where fp returns `_Owned` must track p as Owned (leak if unfreed),
same as a direct call. VisitCallExpr must handle the indirect (fnptr) callee's owned return.
**Peers**: VisitCallExpr, F41 (fnptr owned ret/param checks), F28/F29 (indirect-call arg checks).
**Candidates**: 1. **`mk_t fp=mk; int *_Owned p=fp();` unfreed — leak flagged (sound) vs not tracked (FN)** UNPROBED ⭐.
2. owned ARG via fnptr call (move tracked?). 3. fnptr returning owned, result discarded (temp leak).

## OwnershipImpl::merge (BSCOwnership.cpp:231-~320) — read 2026-06-25 (PRECISE F75 ROOT)
INVARIANT (intended): the CFG dataflow join for ownership state should MEET — a field/var is "owned" at a join
ONLY if owned on ALL predecessor paths (moved on any path ⇒ moved at join). ACTUAL (BUG=F75): the merge UNIONs:
- OPSStatus/SStatus/BOPStatus bitvectors: `statsA.OPSStatus[VD] |= BV` (BitVector OR, :245/:271/etc.) — a set bit
  = owned; OR ⇒ owned-in-EITHER-predecessor ⇒ a field moved on one branch (bit clear) but owned on another (bit
  set) becomes owned at the join → moved-state LOST.
- OPSOwnedOwnedFields/SOwnedOwnedFields sets: union via `.insert` (:257-259/:284-285) — same.
- :233 `if (statsA.empty()) return statsB;` first-predecessor seed (cf F26 empty-side asymmetry).
→ a field freed/moved on one branch is RESTORED to owned at the join → no use-after-move/leak fired → silent
DOUBLE-FREE (F75, re-validated open 2026-06-25). FIX SURFACE: change the OR/union to AND/intersection for the
owned-status bitvectors + owned-field sets (owned-at-join = owned-in-ALL-preds), handling the empty-seed carefully.
CONTRAST: InitAnalysis::merge correctly MEETs (missing field-entry = Uninitialized → MaybeInit) — opposite, correct.
F75 is the sole confirmed defect here; the move/leak/field-assign logic elsewhere is sound (F44/F45/F61/F67 noted).

## checkMemoryLeak (BSCOwnership.cpp:1941-2010) — re-read 2026-06-25 (S2)
INVARIANT: at scope-exit/lifetime-end, emit leaks for a var that still owns: OPSStatus[VD] not Moved/Uninit
(!canAssign) → MemoryLeak (+ reset→Moved, important for loops); OPSOwnedOwnedFields[VD] non-empty → FieldMemoryLeak;
SStatus struct-with-owned: SOwnedOwnedFields non-empty → FieldMemoryLeak (non-owned-struct), or
OwnedStructPartiallyMoved (:1989 owned+null < all-owned & !Moved). OWNED-STRUCT/destructor branch (:1977-1999) = OOS.
CANDIDATES (no new): leak-detection correct; reads OPSOwnedOwnedFields/SOwnedOwnedFields — the F75 double-free is
UPSTREAM (OwnershipImpl::merge corrupts those sets via union), not in this check. checkMemoryLeak SOUND.

## VisitCStyleCastExpr void*_Owned cast-consume (BSCOwnership.cpp:2273) — adjacent-fix re-probe of F91/411b4118 (2026-06-26)
INVARIANT: a cast to `void *_Owned` (the safe_free consume path) must mark the operand's owned var/field MOVED so a
later use/free is flagged. Dispatch on InnerE (after IgnoreParenCastsSafe + comma-RHS peel :2280): DeclRefExpr
(:2289 checkCastOPS/BOP), MemberExpr (:2306 checkCastField), UO_Deref chain (:2319), rvalue hasOwnedFields (:2342, the
F91 fix). PEERS: F118 (temp-leak peels comma not &&/||), checkCastOPS/checkCastField, the move-marking.
CANDIDATES:
1. (ConditionalOperator not handled, UNPROBED top) `(void*_Owned)(c ? s : s)` — InnerE is a ConditionalOperator,
   matches NO arm (not DRE/ME/UO; owned-pointer type has no owned FIELDS so :2342 skips) → operand NOT marked moved →
   later safe_free(s)/use(s) not flagged → DOUBLE-FREE FN. (Comma IS peeled :2280 but ternary is not.) Probe.
2. (LAnd/LOr operand, UNPROBED) `(void*_Owned)(c && s)` similar — no logical-op peel.
3. (StmtExpr / other wrappers, OOS) skip.

## HandleInitListExpr (BSCOwnership.cpp:2412) — init-list owned-field state (refactor 17a0c9c, adjacent re-probe 2026-06-26)
INVARIANT: after `struct S s = {.f = <init>}`, each owned field's state (owned/null) must be tracked so an unfreed
owned field leaks (diagnosed) and a null field is leak-exempt. Branches: null/ImplicitValueInit→markAsNull (:2441);
nested InitListExpr→recurse (:2456); IsCastFromVoidPointer & tracked→mark OWNED (:2465). NO branch for other owned
initializers (a call/var returning _Owned).
PEERS: F23 (double-owned null-init, fixed c25ff17), F32/F47 (compound-literal owned temp, fixed), checkMemoryLeak.
CANDIDATES:
1. (owned-call init not marked owned, UNPROBED top) `{.f = mk()}` (mk returns _Owned, not a void-cast) → IsCastFromVoid
   Pointer false → field NOT inserted into SOwnedOwnedFields → if s.f never freed, leak may be MISSED (FN). Probe vs safe_malloc.
2. (moved-var init) `{.f = o}` (o an _Owned local moved in) → same: not void-cast → field-owned not tracked?
3. (designated partial + owned-call) `{.f = mk(), .g = 0}` mix.

## checkBOPUse / checkBOPFieldUse (BSCOwnership.cpp:1549/1591) — borrow-context var use (2026-06-26 Mode-1)
INVARIANT: a use of var VD in borrow context emits move/uninit/partial-move diag if VD isn't Owned (:1554-1573);
`&_Mut` of a Null VD promotes it to Owned (:1574-1577, the init-through-mut-borrow pattern); a value-use (!isGetAddr)
sets VD Moved + resetAll (:1579-1586).
PEERS: checkSUse/checkOPSUse (state-check siblings), checkMemoryLeak, F44/F45/F61/F67 (checkS family).
CANDIDATES:
1. (&_Mut-Null→Owned without write, probe) does promoting a null var to Owned on &_Mut cause a leak-FP/free-FN if
   the borrow never writes? (likely benign: free of null = no-op, conservative-safe).
2. (PartialMoved use → resetAll Moved, :1583) using a partially-moved var marks ALL fields Moved — un-moved fields
   now considered moved; could that leak them (no free required)? RANK MED, probe.
3. (uninit not set Moved, :1581) using uninit VD doesn't set Moved — double-diag possible, low.

## checkOPSAssign / checkOPSDerefAssign / checkOPSFieldAssign (BSCOwnership.cpp:1105/1139/1175) — assign-to-owned (2026-06-26 Mode-1)
INVARIANT: assigning to an owned-pointer VD is valid only if VD is uninit/moved (canAssign); else the live value
would leak → InvalidAssignOfOwned (:1112) / PartialMoved/AllMoved variants. After assign, VD→Owned, all fields owned
(:1130). PEERS: checkOPSUse, checkMemoryLeak, the overwrite-leak test (PROBED-SOUND: `o=mk()` over live o → caught).
CANDIDATES: 1. canAssign() correctness — verified sound (overwrite caught). 2. `OPSOwnedOwnedFields=OPSAllOwnedFields`
(:1130) marks ALL fields owned after assign — sound if the assigned value owns all fields. 3. no obvious gap; the
assign-to-owned leak-prevention is the load-bearing check, confirmed sound.

## checkBOPFieldUse (BSCOwnership.cpp:1591) — borrow-context field use (2026-06-26 Mode-1)
INVARIANT: a borrow-context field use checks the field's PARENT chain (substrings of fullFieldName) — if a parent is
AllOwned but not currently Owned (moved) → InvalidUseOfMoved (:1597-1606); then VD uninit/may-uninit → InvalidUseOf
Uninit/PossiblyUninit. Sound field-level move/uninit tracking, consistent with checkOPS*/checkS*/checkSField families
(all PROBED-SOUND). The owned-field check families (OPS/S/BOP × Use/Assign/FieldUse/FieldAssign/DerefAssign) are now
all read — uniform per-field move/uninit/leak tracking; the one KNOWN field bug is F75 (the MERGE union, not these checks).

## TransferFunctions::Visit* dispatch coverage (BSCOwnership.cpp, 2026-06-27 Mode-1)
Visitors: AbstractConditionalOperator (covers BOTH standard ternary AND GNU `?:` at AST level), ArraySubscript, Binary,
Call, CStyleCast, DeclRef, DeclStmt, InitList, LifetimeEnds, Member, Return, Stmt, UnaryExprOrTypeTrait, UnaryOperator.
CompoundAssignOperator → falls to VisitBinaryOperator (base). NO missing-visitor gap. NOTE: AST-ownership handles
AbstractConditionalOperator, so F120 (GNU `?:` double-free) is NOT a wholesale missing visitor — it's the OpaqueValueExpr
shared-operand move not propagated (spans AST + IRBuilder paths; `p ?: mk()` slips, standard `c?p:q` arm-move is tracked).

## TransferFunctions::VisitBinaryOperator + HandleDREAssign + checkBOPAssign (BSCOwnership.cpp:2186, 2513, 1670) — PROBED-SOUND/FOLDED (Chain C re-walk @34883aa1, 2026-06-29)

**Outcome (2026-06-29 cycle, 8 probes):** candidate-1 (state-update-after-diag) SHAPE-REJECTED
(canAssign:417 diag rejects compilation before the unconditional set-Owned can mask a leak);
the `RHS->isNullExpr` null-launder direction (:2201) FOLDS into **F108**; `checkBOPFieldAssign:1714`
write-after-move parent-walk FOLDS into **F110 blast-radius**. void-cast recovery → AllMoved (:2204)
is `_Unsafe`-only (safe-zone conversion gate blocks it). Whole-struct owned-field copy `s2=s1`,
owned-field assign through a `_Mut` borrow, moved-var-to-`_Bool`-param — all SOUND or shape-rejected.
Cross-analyzer hop did not separate a new disagreement; the obvious null/ownership splits all fold
into F108. See _probed.md 2026-06-29 for the per-probe ledger.

**Invariant**: a `LHS = RHS` assignment (a) consumes RHS ownership ONLY when LHS is a
tracked type (`op = IsTrackedType(LHSType) ? Move : GetAddr`, :2192-2194), then (b) re-marks
LHS Owned via the check{OPS,S,BOP}Assign family routed by HandleDREAssign by which
*Status map* contains the dest VD (OPSStatus=owned-ptr, SStatus=struct-with-owned-field,
BOPStatus=borrow-ptr). The post-step `setToNull`/`setToAllMoved` (:2201-2205) handles
`p = NULL` and `p = (void*_Owned)q` specially.

**Peers**: Nullability::VisitBinaryOperator (re-walked SOUND @34883aa1), Init::transferStatement
Assign-case (:159), Borrow DefUse/ActionExtract::VisitBinAssign. checkOPSAssign (:1112),
checkSAssign, checkBOPAssign (:1670 — all 3 follow the same canAssign→diag→resetAll→setOwned
shape).

**Candidates**:
1. (cross-analyzer, HIGH) RHS-consume gate keyed on LHS TYPE not RHS type: `op = IsTrackedType(LHSType)`.
   When LHS is tracked but RHS is an owned value bound differently, or where the dest is
   re-marked Owned (:1696 unconditional `set(VD, Owned)` after a diag) — does the LHS get
   credited Owned even when the assignment was REJECTED (canAssign false → diag emitted but
   state STILL set to Owned at :1694-1696)? If a later free of that LHS is then accepted, the
   state-update-after-diag could mask a leak/double-free on the recovery path. Probe: assign to
   an already-Owned var (InvalidAssignOfOwned diag) — is the OLD owned value leaked AND new
   tracked? checkBOPAssign unconditionally overwrites BOPOwnedOwnedFields then set Owned.
2. (compound-assign, MED) `p += i` / `p -= i` for owned ptr: isAssignmentOp() true → treats it
   like a fresh assign (resetAll+setOwned) but the LHS is mutated-in-place not reinitialized.
   Sema likely rejects owned-ptr arithmetic (shape-reject risk). Check struct-with-owned-field
   `s.f += ...` shape.
3. (self-assign, MED) `p = p` for owned ptr: RHS visited op=Move (HandleDREUse marks p moved),
   THEN LHS visited op=Assign (re-marks p Owned). Net: Owned. But a real self-move could
   double-free at runtime. Likely benign (no runtime drop on plain assign) — verify.

## OwnershipImpl::MaybeSetNull (BSCOwnership.cpp:2662-2683) — UNPROBED (2026-06-29 chain K/E re-walk pivot)

**Invariant**: at each CFG join edge, ownership consumes the SHARED `NullCheckInfo`
producer (same one nullability uses) to mark `_Owned _Nullable` pointers null in the
branch where the condition proves them null — true branch uses `nullCheckedExprs`
(→setToNull), false branch uses `presentCheckedExprs` (→setToNull). Resetting to Null
SUPPRESSES the leak/double-free check on that branch (setToNull → resetAll + Status::Null).
The producer's null/present classification MUST be sound: if ownership setToNull's a
pointer on a branch where it is actually still OWNED, the scope-exit leak check is
silenced → silent leak (FN); if it fails to setToNull a genuinely-null pointer, a
double-free could be miss-suppressed (FP).

**Peers**: nullability `PassConditionStatusToSuccBlocks` (BSCNullabilityCheck.cpp:783) —
the OTHER consumer of the same `NullCheckInfo`. They must agree which branch is True/False
and which exprs are null vs present. F108 (isNullExpr divergence ownership vs nullability)
is a sibling — ownership's null-classification path differing from nullability's.

**Candidates**:
1. **Composition / asymmetry** — `MaybeSetNull` builds `NullCheckInfo` from `cur->getLastCondition()`
   (block-local leaf), uses `succ_begin()[0]`/`[1]` for true/false. If a compound condition
   (`&&`/`||`/`!`) makes the producer put an `_Owned _Nullable` ptr in the WRONG set for a
   branch, ownership setToNull's a still-owned ptr → leak FN. The De Morgan `init` recursion
   (post-F70) feeds both consumers; a residual mis-classification = soundness divergence.
2. **Reachability** — `setToNull` resets owned-field bookkeeping; if a partially-moved
   `_Owned` struct field is narrowed, setToNull clears OwnedOwnedFields → a subsequently-needed
   free is skipped → leak.
3. **Symmetry** — the false-branch arm uses `presentCheckedExprs`; if presentChecked is
   computed for the wrong sub-expr in a `p != q` / assignment-in-cond form, the null-suppression
   fires on a live owned ptr.

Top: candidate #1 (compound-condition narrowing of _Owned _Nullable → wrong-branch setToNull → leak FN).

## HandleInitListExpr (BSCOwnership.cpp:~2410-2476) — aggregate-init per-field ownership — read 2026-06-29
**Invariant**: for `struct S s = {...}`, iterate fields; a field whose init is null (`isNullExpr`/ImplicitValueInit) → markAsNull (remove from owned set); a nested-record-field init → recurse; a void*-cast init → mark owned. A field defaulting from SAllOwnedFields stays owned unless markAsNull fires. **Peer/Chain**: line 2441 `FieldInit->isNullExpr(OS.ctx)` is a **Chain AA** consumer (ownership's syntactic isNullExpr vs nullability's flow-sensitive classification). **Candidates**:
1. (Chain AA / F108 in init-list, PROBE) `struct S s = { nullVar }` where nullVar is a null-owned VARIABLE → isNullExpr(DeclRefExpr)=false (F108 gap) → field NOT marked null → stays owned → FP leak at scope exit.
2. ConditionalOperator field init `{ c?n:n }` (isNullExpr no ?: arm) — same fold.
3. nested-record null propagation depth.

## OwnershipImpl::merge (BSCOwnership.cpp:~230-310) — ownership-state CFG join (F75 root)
- **Invariant**: at a join, a field is "definitely owned/live" (must-free) only if owned on ALL predecessors; a moved field on ANY predecessor must stay moved (MEET semantics for the owned-field set).
- **Peers**: runOnBlock (calls merge over preds), checkS*/checkOPS* (consume the merged field sets), SOwnedOwnedFields/SNullOwnedFields/SAllOwnedFields.
- **Structure**: UNIONs every field-set component — OPSOwnedOwnedFields (insert, 254-263), SOwnedOwnedFields + SNullOwnedFields + SAllOwnedFields (insert, 276-298), BOPStatus (300+) — and ORs the status bitvector (`SStatus[VD] |= BV`, 271). A MAY-merge throughout.
- **Candidates**: (1) **PROBED-F75 (filed, broadened this session)**: owned-field-set UNION restores a field moved on one path → post-join consume double-frees; manifests at if/else (filed repro) AND loop back-edge (broadened 2026-06-30). One MEET fix for the owned-field sets closes both. (2) the SNullOwnedFields union + the SStatus `|=` are part of the same may-merge — a field null/owned on divergent paths lands in both sets; folds into the same F75 meet-vs-union fix. (3) **PROBED-SOUND 2026-06-30 (nullability-backstopped)**: field owned on one path, null on the other (`if(c){s.v=mk();}else{s.v=nullptr;} consume(s.v)`) → REJECTED "cannot pass nullable pointer argument" (the merged maybe-null is caught by the nullability check, which also blocks the c-true leak path). No distinct observable beyond F75; the union-merge of SNullOwnedFields does not produce a separate FN here.

## TransferFunctions::VisitCallExpr (BSCOwnership.cpp:2244) — move-on-call
- **Invariant**: passing an `_Owned` arg MOVES it (op=Move); a `_Bool`-typed arg does NOT consume (op=GetAddr); a cast-from-void-owned arg with owned fields → PassCastToArgOrRet diag.
- **Peers**: VisitCStyleCastExpr (void-cast), VisitBinaryOperator (assign-move), F101 (owned→_Bool gate), IsCastFromVoidPointer (chain AF, OOS).
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: `if(p){} consume(p)` and `if(!p){} consume(p)` → rc=0 — owned→bool in a CONDITION does NOT consume p (bool-typed arg → GetAddr → no move); p survives. Sharpens F101: the condition form works; only the `_Bool b = p` VARIABLE-init is rejected by the safe-zone gate (spec rule 13 allows it) → F101's FP is that narrow bool-var-init case, not the move semantics. (2) void-cast PassCastToArgOrRet diag — OOS (take_from_raw). (3) `isHandlingCallExpr` re-entrancy guard — nested-call coverage.

## TransferFunctions::HandleDREUse + VisitDeclRefExpr (BSCOwnership.cpp:~2064) — ownership use-check dispatch
- **Invariant**: a READ of an owned var/field is use-checked (checkOPSUse etc.) — but ONLY when `op == Move || op == GetAddr`; op is set by the parent context.
- **Peers**: HandleDREAssign (op==Assign), VisitCallExpr (sets Move), F111 (bare-stmt read = op None → skipped).
- **Candidates**: (1) **F111 CONFIRMED**: a bare/discarded expression statement read has op=None (neither Move nor GetAddr) → HandleDREUse skipped → use-after-move FN. (2) **FOLDED-F111**: an if/while CONDITION read of a moved owned (`consume(p); if(p){}` / `while(p)`) is NOT diagnosed (rc=0) — the condition is the block's last CFGStmt with an excluded top-level kind (ImplicitCastExpr owned→bool), same `runOnBlock` allowlist (2697-2707) as F111's bare-stmt reads; moved-tracking works (double-consume rc=1) & analysis runs (leak caught rc=1), proving it's the visit-filter. Same fix (route all CFGStmts through TF.Visit covers conditions). BROADER than F111's listed bare-statement breadth: also branch/loop conditions. (3) comma/paren discarded reads (fold F111, op=None propagates).
