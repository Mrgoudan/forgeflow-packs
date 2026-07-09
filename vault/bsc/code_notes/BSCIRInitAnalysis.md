# BSCIRInitAnalysis.cpp

Source: `clang/lib/Analysis/BSC/BSCIRInitAnalysis.cpp` (1300 lines).

CFG-based init analysis on the BSCIR (separate IR from the AST-based analyzers). Tracks LocalStates, FieldStates, EnsureInitDerefStates. F15 (LOW) was the only finding here.

## checkEnsureInitAtReturn (BSCIRInitAnalysis.cpp:1511-1543) — read+probed 2026-06-17, PROBED-SOUND

**Invariant**: a callee with `__attribute__((ensure_init))` on `*out` must leave `*out`
Initialized on ALL return paths. Rejects both Uninitialized (EnsureInitNotInit) AND MaybeInit
(EnsureInitMaybeNotInit, :1527-1528). ensure_init_if params checked per-return-path (skip :1516).
**Probed SOUND**: ep1 one-path-init → "may not be initialized on all paths" (MaybeInit caught);
ep2 all-paths → clean; ep3 never-init → "not initialized at return". F100 (live HIGH FN) is the
CALLER-side fnptr-cast laundering of contract COMPATIBILITY, NOT this callee enforcement (sound).

## merge / meetStates (BSCIRInitAnalysis.cpp:680-759) — read 2026-06-17, PROBED-SOUND

**Invariant**: at a CFG join, each tracked place's state is the MEET of the
predecessors — `meetStates(A,B)= A==B?A:MaybeInit` (MaybeInit is top/conservative).
A place initialized on only some incoming paths becomes MaybeInit → use is diagnosed.

**Asymmetry spotted (C5 candidate) + why it's benign**: LocalStates merge (:692-704)
handles only "missing in Dst → copy Src" and has NO "present in Dst, missing in Src"
loop; FieldStates (:707-732) handles BOTH sides (meet missing with Uninitialized). A
local Init in Dst but absent in Src would wrongly stay Init (FN). BUT `entryState`
(:128-137) pre-populates EVERY local (Init or Uninit) into LocalStates, and merge never
removes keys → the local key-set is INVARIANT (all locals, every path). So "local missing
in Src" is UNREACHABLE; the simpler LocalStates merge is correct. FieldStates needs the
both-sides logic because fields are tracked LAZILY (key-sets differ across paths).

**Peers**: entryState (:111, the all-locals pre-population that makes the invariant hold);
transferStatement (consumes merged state); meetStates (the lattice meet).

**Candidates**:
1. (C5) LocalStates missing-in-Src FN → SHAPE-REJECTED (entryState pre-populates all
   locals; switch-jump-past-decl probe confirms x carries Uninit on the bypass path →
   join = MaybeInit → use diagnosed). Probed below.
2. (C5) FieldStates lazy-key both-sides meet — handled correctly (:707-732). SOUND by reading.
3. (C7) MaybeInit not propagated to a later narrowing — MaybeInit IS top, propagates. SOUND.

## State model

`InitLattice` has three maps:
- `LocalStates`: LocalId → InitState (Uninit/Init/MaybeInit)
- `FieldStates`: FieldPath (LocalId + Indices) → InitState
- `EnsureInitDerefStates`: LocalId → InitState (for ensure_init params, tracks pointee init state)

## Functions

### `entryState` — :47-86
**Invariant**: pre-populates **all** locals with initial states. Params (1..NumParams) → Initialized + all fields init. Other locals → Uninitialized unless implicitly-init (globals/statics/va_list). EnsureInitDerefStates pre-populated for params with `EnsureInitAttr`.
**Side note**: this pre-population means merge() can never see a "missing LocalId" — both Src and Dst always contain every local from the start.

### `transferStatement` — :88-224
**Invariant**: applies one IR Statement to the lattice. Cases:
- **Assign** to local (whole) → Init
- **Assign** to FieldPath → markFieldInit (handles union variants specially)
- **Assign** to `*p` / `p->field` → callee-side ensure_init updates (for ensure_init params)
- **StorageLive** → Uninit (or Init for implicit-init types)
- **StorageDead** → Uninit
**Documented limitation** (:152-154): array element writes do NOT mark the array as Init. Arrays must be initialized via init-list or `__assume_initialized`. Probed: `int arr[3]; arr[0]=…; arr[1]=…; use(arr[0]);` → use-of-uninit diag (intentional per comment).

### `transferTerminator` — :226-339
**Invariant**: handles call terminators (regular calls + `__assume_initialized` builtin). For `__assume_initialized`, decodes the AddrOf-pattern argument (line 245-277). For other calls with ensure_init params, marks the addressed place as Initialized in the caller (line 281-332).
**Coverage**: ArgPlace handled for `Projections.empty()` (whole local), `getFieldPath` (struct fields). No handling for Index projections (array element passed via `&arr[0]` to ensure_init) — but per the documented array limitation, this is consistent.

### `meetStates` / `merge` — :343-413
**Invariant**: meet of `==` returns A; else MaybeInit. merge is asymmetric in code but symmetric in effect because:
- LocalStates: only Src→Dst loop; missing-from-Dst (Src has, Dst doesn't) sets Dst = Src directly without meet. **Should be meet(Src, Uninit) per fallback semantics**. But entryState pre-populates all locals, so missing-from-Dst never happens → asymmetry is unreachable.
- FieldStates: explicitly handles BOTH directions (lines 369-395). Symmetric.
- EnsureInitDerefStates: same asymmetric pattern as LocalStates, but also pre-populated → unreachable.

### `checkOperand` — :829-938
**Invariant**: emits a diag for each read of an Uninit/MaybeInit place. Returns early for Init operands. Special-case for union-struct-variant reads (line 844-869) and cross-variant reads (line 880-888) and ancestor-init checks (line 892-905). Whole-local check at line 924-937.

### `checkEnsureInitAssign` — :1019-1077
**Invariant**: enforces the ensure_init pointer-aliasing rule. If an ensure_init param's `*p` is not yet initialized, the pointer can't be reassigned and can't be aliased into a named variable (only temps tracked for same-block deref-check).

## Top probing candidates (none confirmed; speculative)

1. **C5 LocalStates merge asymmetry** — **SHAPE-REJECTED** by entryState's full pre-population.
2. **Nested union variant tracking** — `isUnionStructFieldPath` returns false for nested unions (line 706 requires variant to be struct, not union). Possible edge case but BSC's nested union usage is rare.
3. **Array fields in ensure_init pointers** — checkOperand and transferStatement skip array-typed field handling. Consistent with documented array limitation.

## Not yet read

- `meetStates` semantics under various combinations (line 343-349)
- `markFieldInit` / `tryPromoteParent` / `clearFieldStates` helpers
- `hasUnionFieldEntries` / `clearUnionFieldEntries` (union variant entry tracking)
- ~`checkEnsureInitDerefReads`~ — READ
- ~`checkEnsureInitAtReturn`~ — READ 2026-05-20
- `run` entry point at line 1160 — READ via 1160-1287 earlier

### `checkEnsureInitAtReturn` — :1139-1154
**Invariant**: at return points, every ensure_init param's pointee must be Initialized. If Uninitialized → `EnsureInitNotInit`. If MaybeInit → `EnsureInitMaybeNotInit`. Initialized → no diag.
**Iterates**: `State.EnsureInitDerefStates`. Per `entryState` (:76-83), only `ParmVarDecl`s with explicit `EnsureInitAttr` are pre-populated. Indirect calls via function pointer with ext-param ensure_init info may NOT have direct attrs — possibly missed at return-check time.
**Candidates ranked**:
1. **C3 indirect-call ensure_init at return** — UNPROBED. Function pointer typed with ensure_init param. The local that holds the indirect-call's "this arg" position might not have EnsureInitAttr directly. Need: a function pointer call where the pointer-param type has `__attribute__((ensure_init))`; the wrapper passes a partially-initialized arg.
2. **C5 partial struct init through pointee at return** — partial probe done; analyzer catches missing field init via dataflow. Standard.

## Status

The init analysis is the most thoroughly designed of the four analyzers. F15 is the only finding (LOW). Most candidates here have been **SHAPE-REJECTED** by entryState pre-population or documented intentional behavior. Further reading is high-cost low-yield without a fresh defect-class hypothesis.

## Cycle 13: transferStatement, checkOperand, getFieldPath, forEachRvalueOperand

### transferStatement (line 88-224)

**Invariant**: dispatch on Statement kind (Assign/StorageLive/StorageDead/Nop). For Assign:
- Local destination → mark Initialized.
- FieldPath → check union-struct-variant + union-variant + plain struct, markFieldInit accordingly.
- Deref destination via ensure_init → markPointeeFullyInit or per-field via SubPlace.
- Array element writes intentionally NOT tracked (require __assume_initialized).

**Reachability**:
- Line 158: special path fires only when first projection is Deref. Index/Field-first projections handled elsewhere.
- Line 165: deref-then-field only works with ensure_init pointer. Non-ensure-init borrow pointee field writes are not tracked.

**Probed**:
- `&_Mut x` where x uninit → CORRECTLY rejected ("use of uninitialized value").
- `*b = val` where b borrows uninit → blocked at borrow creation.
- `(*b).y = X` where b borrows fully-init struct → standard tracking.
- `writer(__attribute__((ensure_init)) struct S *_Borrow s)` with partial-init caller → caller passes uninit, writer must fully init via ensure_init contract. Robust.

### forEachRvalueOperand (line 945-972)

**Invariant**: iterates all Operands within an Rvalue. Handles 6 of 11 Rvalue kinds (Use, BinaryOp, UnaryOp, Cast, Aggregate, Array).

**Reachability**: kinds NOT visited: Ref, AddressOf, NullPtr, SizeOf.
- NullPtr / SizeOf: no operand, default is correct.
- Ref / AddressOf: payload is a Place (not Operand). Init-state of the Place's source is checked separately (via transferStatement / Place projection walking). The default skip in forEachRvalueOperand is correct — only Operands are iterated here.

### getFieldPath (line 484-515)

**Invariant**: extracts a FieldPath from a Place if first projection is Field. Walks consecutive Field projections.

**Reachability**: returns None for first-projection-Deref or Index. For deref-then-field paths, callers slice past the Deref (line 168 in transferStatement) and re-call getFieldPath on the sub-place — this is correct.

### Cycle 13 conclusion

Init analysis is thorough on tracked locals + ensure_init pointees. Field-level read of partial struct correctly diagnosed. Borrow + field-write through deref correctly tracked (with init-source requirement). No new defect surface identified.

## Cycle 14: checkEnsureInitAssign, checkEnsureInitDerefReads, checkEnsureInitAtReturn

### checkEnsureInitAssign (line 1019-1077)

**Invariant**: enforces ensure_init contract on local assigns. Rejects:
1. Reassigning an ensure_init-param local before *param has been initialized.
2. Aliasing (copying) an ensure_init param to a named local before fulfillment. Temp copies are tracked but not rejected.

**Reachability**:
- Line 1049: skips non-Use Rvalue (Ref, AddressOf, Cast, BinaryOp, etc.). Alias creation via non-Use forms could potentially escape. But most such forms are type-level rejected (e.g., `_Borrow` to `_Borrow` of `_Borrow` is forbidden) or are structurally inconsistent with the alias pattern.

### checkEnsureInitDerefReads (line 1079-1110)

**Invariant**: rejects reads through *ensure_init_param before the contract is fulfilled.

### Cycle 14 conclusion

Ensure_init contract enforcement is robust. Conflict between init-analyzer (allows uninit pointee through ensure_init contract) and ownership-analyzer (treats `&_Mut p` of uninit p as a "use of uninitialized value") is documented (probed.md line 834) — two analyzers disagree, but ownership-analyzer's diag fires first. Known limitation, not a new defect.

## 2026-05-21 Negative-corpus wrapper enumeration (8 probes, no new pattern)

Goal: identify a wrapper kind that lets a USE of an uninit local/field slip past `checkOperand`'s diag site. Method: take each `expected-error` line in `clang/test/BSC/Negative/InitAnalysis/*.cbs` and wrap the offending sub-expression with one of {ParenExpr, CStyleCastExpr, CompoundLiteralExpr, BO_Comma, ConditionalOperator-arm, ArraySubscript, MemberExpr-base}.

All probes diagnosed correctly. The InitAnalysis is a BSCIR-based dataflow; lowering goes through `BSCIRBuilder::Visit*` which has overrides for all the wrappers tested. The wrapper expressions get lowered to the same Operand-on-Place that `checkOperand` reads, so every wrapper preserves the uninit-state check. Distinct from the AST-based analyzers (CheckTemporaryVarMemoryLeak / getMemberFullField / etc.) where wrapper-strip omissions are the common defect surface.

- `(int){x} + 1` for uninit `x` — caught (INCONCLUSIVE)
- `(0, x) + 1` (BO_Comma RHS) — caught (INCONCLUSIVE)
- `c ? x : 0` (ConditionalOperator arm) — caught (INCONCLUSIVE)
- `va_arg(ap, int)` with uninit `va_list` — SHAPE-REJECTED (va_arg/variadic forbidden in `_Safe`)
- `arr[i]` for uninit `arr` — caught (INCONCLUSIVE; baseline negative test already covers)
- `&_Mut arr[i]` for uninit `arr` — caught (INCONCLUSIVE)
- `s.inner.b + 1` after `s.inner.a = 1` (nested partial init) — caught at b correctly (INCONCLUSIVE; F55-cousin probe shows partial-init of `s.inner.a` doesn't launder `s.inner.b`)
- `(int)(x + 0)` (CStyleCast wrap of arith on uninit) — caught (INCONCLUSIVE)

Conclusion: the BSCIRBuilder's Visit-override coverage for in-scope wrapper kinds is comprehensive; uninit-state propagates through every wrapper that lowers to an Operand. No new defect class from this slice.

Frontier hint for next session: try **EnsureInit/EnsureInitDerefStates** with chained-pointer arguments under wrapper variations (`__assume_initialized(&(s.f))` with paren around the field, etc.). The Sema-side `__assume_initialized` checker at SemaChecking.cpp:2302 demands bare `&` syntactically, but the BSCIR-side `transferTerminator` handler at lines 245-277 may have looser AST-recognition for ArgPlace decoding.

## isUnionStructFieldPath (BSCIRInitAnalysis.cpp:691-731) — READ 2026-05-22

**Invariant**: walks a FieldPath; returns true iff the path traverses a union→struct-variant→deeper transition. Sets UnionDepth to the union level.

**Peers**: `tryPromoteParent` (line 536) — uses UnionDepth to special-case union variants in upward init promotion. `markFieldInit` (line 526) — initial caller.

**Candidates**:
1. Out-of-range index in FP.Indices doesn't match any field — inner for-loop falls through without setting CurTy; outer for-loop continues with stale CurTy. UNPROBED but only reachable from malformed FieldPath (shouldn't occur from valid AST).
2. Union with ZERO fields (e.g., empty union, anonymous) — inner loop empty; CurTy unchanged. Probably SHAPE-REJECTED by Sema.
3. Nested union of union → variant: when the variant of an outer union is ALSO a union, the function continues walking with CurTy set to the inner union. The next iteration treats the inner union the same way. Correct behavior for arbitrary nesting depth.

Top: candidate #1. Low-probability, low-impact (malformed input only). UNPROBED.

## markPointeeFullyInit (BSCIRInitAnalysis.cpp:624-638) — READ 2026-05-22

**Invariant**: when a function returns from an ensure_init contract OR when `__assume_initialized` runs, marks the entire pointee tree of a base as Initialized.

**Peers**: `markAllFieldsInit` (line 640) — recurses through nested struct/union variants. `__assume_initialized` handler (line 244-260) — caller for the Sema-only builtin.

**Candidates**:
1. **`__assume_initialized` arg-dropping (F58 territory)**: marker still does its work, but the argument's side effects don't run. Already filed.
2. **Sibling projections leak through Deref handling**: in __assume_initialized handler, line 256-269 handles `&*p` and `&p->f...` paths. Verify the projection-slice approach correctly distinguishes Field-only vs mixed.
3. **markAllFieldsInit for unions** (line 640-): walks all fields. For unions, walking ALL fields means marking ALL variants — possibly over-promotion (only one variant is active at a time). UNPROBED but reasoned-okay since the function is in the "fully init" path, marking "all" is conservative-correct.

Top: candidate #2. UNPROBED; speculative.

## transferStatement — Assign whole-local case (:96-103) — 2026-05-29
**Invariant**: an Assign whose Dest is a whole local marks that local `Initialized`
unconditionally (no sub-field marking); the SOURCE rvalue's init is checked
separately by checkOperand. Field-dest assigns go through markFieldInit (:150).
**Peers**: StorageLive implicit-init path (:192 markAllFieldsInit) — DOES mark
fields; the whole-local Assign does NOT (relies on whole-LocalState dominating
field reads).
**Candidates**:
1. **FN — whole-struct copy of a PARTIALLY-init source**: `struct S s1; s1.a=5;
   struct S s2 = s1; read s2.b;` — s1.b is uninit; the whole read of s1 in
   `s2 = s1` should flag use-of-uninit-field. If the whole-struct copy launders
   it (Src checked only at whole-local granularity), the uninit read is missed. UNPROBED → probing now.
2. Whole-local Assign doesn't markAllFieldsInit on Dest (vs StorageLive:192);
   stale field-uninit masked by whole-init — safe direction.
3. ensure_init through *param (:159-176) — partial.

## ensure_init callee-exit contract (checkEnsureInitAtReturn :1140 + merge :398) — PROBED-SOUND 2026-05-29
**Invariant**: a param `int *__attribute__((ensure_init)) out` requires `*out`
(all fields, all paths) initialized before every return; else EnsureInitNotInit
(Uninit) / EnsureInitMaybeNotInit (MaybeInit at merge). Entry seeds
EnsureInitDerefStates[param]=Uninitialized (:80); merge meetStates per param (:405).
**Probed SOUND (5 cases, no bug)**: asymmetric branch-init → "may not be initialized
on all paths"; never-init → "not initialized at return"; all-paths-init → clean;
struct-pointee partial field (`out->a` only) → "not initialized at return";
struct-pointee full → clean. The EnsureInitDerefStates merge is conservative
(meetStates), and field-level tracking is integrated. NOTE: the merge (:398-411)
only meets entries present in BOTH; Dst-only entries aren't met-with-Uninit
(unlike FieldStates :388-395) — but UNREACHABLE since every ensure_init param is
seeded at entry, so both states always carry the entry. No FN.

## forEachRvalueOperand (BSCIRInitAnalysis.cpp:945-973) — PROBED-SOUND 2026-05-29
**Invariant**: visit value-reading operands of an Rvalue (for init/ensure_init operand checks).
Rvalue::Kind = {Use,Ref,AddressOf,BinaryOp,UnaryOp,Aggregate,Array,Cast,NullPtr,SizeOf} (BSCIR.h:435).
Handles Use/BinaryOp/UnaryOp/Cast/Aggregate/Array (the 6 value-READING kinds); `default` skips
Ref/AddressOf/NullPtr/SizeOf — provably complete (those don't read their operand's value:
address-taking / no-operand / unevaluated).
**Probed SOUND**: sizeof(uninit) allowed (unevaluated); read-uninit flagged; `g(&x)` of uninit x
to a NON-ensure_init param IS flagged "use of uninitialized value" — NOT via forEachRvalueOperand
(correctly skips AddressOf) but via the separate CALL-ARG init check (callee may read *p). Layered
correctly; the default-skip is compensated. No FN. `/tmp/fe1-3.cbs`. No bug.

## checkOperand cross-variant read + merge interaction (BSCIRInitAnalysis.cpp:878-888 + merge:355-381) — UNPROBED 2026-05-29
**Invariant**: a cross-variant read of a union `u.y` is permitted iff "some variant
was written" — detected via `hasUnionFieldEntries(State, Id, UnionPrefix)` (line 886).
The rule encodes type-pun semantics: if ANY field under the union was written, the
union's bytes are defined, so reading another variant is OK.
**Peers**: merge (FieldStates meet, :355-381), tryPromoteParent (:536 — promotes+clears
on whole-variant write), isUnionVariantPath (:733).
**THE GAP**: `hasUnionFieldEntries` only checks PRESENCE of a field entry, NOT its
init STATE. After a CFG join, a field entry can be present but `MaybeInit` (written on
ONE predecessor only). On the other predecessor the union is entirely Uninit. The
cross-variant early-return (line 887) fires on mere presence → accepts the read even
though on one path the union is uninitialized. The whole-local LocalState meets to
MaybeInit, but checkOperand's first block (line 845 IS==Initialized) is skipped, and the
field block's cross-variant escape (886-888) fires before the MaybeInit diag.
**Candidates**:
1. **C5 FN — cross-variant read of MaybeInit union after if/else** — union with a struct
   variant; write `u.s.a` (partial, no promotion) on the `if` arm only; read int variant
   `u.x` after join. hasUnionFieldEntries=true (entry `u.s.a` present, MaybeInit) →
   read accepted; but branch-else path leaves union uninit → use-of-uninit FN. PROBING NOW.
2. tryPromoteParent clears entries on whole-variant write → would erase the entry, so the
   partial-write (no-promotion) variant is required to keep the entry alive. Covered by #1.
3. Distinct from F75 (ownership MERGE union, different file/analysis) and F55 (getFieldPath
   truncation). This is checkOperand's union cross-variant escape ignoring merge-produced
   MaybeInit. New root cause if confirmed.

### RESOLUTION of the cross-variant candidate (2026-05-29) — SHAPE-REJECTED
The candidate above is REAL in the code (checkOperand:886 hasUnionFieldEntries checks
PRESENCE not init-STATE; transferStatement:118-122 over-promotes the whole union local on a
single sub-field write) but is UNREACHABLE for diagnostics:
- Union field access (read OR write) ALWAYS requires an `_Unsafe` block (Sema: "access to
  union field is _Unsafe and requires _Unsafe block"). So no union read happens in plain _Safe.
- An `_Unsafe{}` block FULLY SUPPRESSES the init-use diagnostic (verified: `_Safe` fn, uninit
  plain int read inside `_Unsafe{}` is accepted; outside it is rejected). _Unsafe is the
  by-design safety escape hatch.
⇒ The union-variant init tracking is dead w.r.t. producing a diagnostic. SHAPE-REJECTED.
Also re-confirmed SOUND on the reachable struct surface: loop-body field-init read-after-loop
and switch case1-init/case2-fallthrough-read are both correctly rejected (MaybeInit). The
InitLattice merge uses meetStates and demotes one-path-init fields/parents to MaybeInit —
it does NOT exhibit F75's union-the-set bug. This analysis is the conservative sibling of F75.

## E2 init×move (transferStatement Assign — Index-projection dest) — 2026-05-30 CONFIRMED-new (pending file)

**Structural fact (move re-uninit is a non-event for init analysis):** transferStatement
(:88-221) has NO move-out re-uninit. Operand::Move == Operand::Copy (:986, :1003 — both
only verify the source is init before READING; neither lowers it afterward). Only
StorageLive (:177) / StorageDead (:203) set Uninitialized; tryPromoteParent (:536) only
PROMOTES, never demotes. So the init lattice is monotone-up; use-after-move soundness
rests entirely on the ownership analyzer (F67/F64), not here. Verified SOUND: whole-struct
copy from partially-init source (whole-local read fires), loop-scoped conditional field
init (MaybeInit), borrow-write-not-tracked (FP-only, backstopped at `&_Mut`).

**THE GAP — `s.p[i] = X` write through an UNINITIALIZED pointer-typed struct field is
accepted in pure `_Safe` → runtime store through a garbage pointer (SEGFAULT).**
- Site: transferStatement Assign case (BSCIRInitAnalysis.cpp:94-174). For Dest = `s.p[i]`,
  the Place is `Base=s, Projections=[Field(p), Index(i)]`. This is handled by NEITHER:
  - the `getFieldPath` branch (:104) — getFieldPath (:470-476) returns None because
    `FP->Indices.size() != Projections.size()` (path isn't pure-Field; ends in Index), nor
  - the Deref-first ensure_init branch (:159) — `Projections[0].K` is Field, not Deref.
  So the pointer operand `s.p` that must be LOADED to form the lvalue `s.p[i]` is never
  fed into the uninit-USE check on the assignment-dest side. The READ form (`x = s.p[i]`)
  routes the same load through checkOperand (operand iteration) which DOES catch it.
- Asymmetry confirmed: `s.p[i] = 5` (uninit p, write) ACCEPTED → vg SEGFAULT; `x = s.p[0]`
  (uninit p, read) REJECTED "use of uninitialized value: s.p"; positive control `s.p=buf;
  s.p[c]=5` clean. Deref siblings `*s.p = 5` and `s.p->x = 5` BOTH correctly rejected —
  isolating the gap to the **Index** projection on the LHS.
- DISTINCT from F55 (FIXED): F55 was the **Deref** path over-crediting `s.p`→Init (getFieldPath
  truncation + unconditional markFieldInit), and F55's first diag (bad deref-of-uninit) DID
  fire. Here `s.p` stays Uninit and NO diag fires at all; the symptom is a hard SEGFAULT, not
  a downstream laundering. Different projection kind (Index vs Deref), different mechanism
  (missing operand-check vs wrong markFieldInit), different symptom.
- Defect class: C6 (localized uninit-use check skipped on a projection-wrapper variant) /
  C3 cousin (an AST/projection kind the dest-side handling doesn't cover).
- Repro: /tmp/explorer_probe.qYOBhO.cbs (runtime SEGFAULT). Baselines:
  /tmp/explorer_baseline.Y7RkA4.cbs (read form rejected), /tmp/explorer_baseline.YSJm1O.cbs
  (init-first positive control clean). Ledger: /tmp/probed_E2.md.

## R2 dest-projection cells — Chain W continuation (2026-05-30) — UNPROBED at start

**Hunt**: F83 = dest Place `[Field, Index]` (`s.p[i]=v`, `s.p` uninit). Which OTHER
dest-projection shapes skip the loaded-base uninit-use check at transferStatement
Assign (:94-174)? Routing predicates that decide coverage:
- `getFieldPath(Dest)` (:104) fires only if Dest is a PURE Field chain
  (`getFieldPathPrefix` walks Field-only at :493-504 + `Indices.size()==Projections.size()`
  at :473). Any Index/Deref in the path → None → no field-init mark, but ALSO no
  base-load check (the gap).
- Deref-first ensure_init branch (:159) fires only if `Projections[0].K == Deref`.
  Handles `*p=v` (markPointeeFullyInit) and `(*p).f=v` via SubPlace (:166-171).

**Per-cell routing analysis (which branch, FOLD vs DISTINCT-candidate):**
1. `s.arr[i].f = v` `[Field, Index, Field]` — getFieldPathPrefix breaks at the Index
   (:501 else-break) → Indices.size()(=1) != Projections.size()(=3) → None. Not Deref-first.
   **Same F83 gap (getFieldPath-None branch miss). FOLD candidate.**
2. `(*pp)[i] = v` `[Deref, Index]`, pp uninit — Projections[0]==Deref → ENTERS the
   Deref-first branch (:159). Projs.size()==2 so NOT the whole-deref `*p` case;
   falls to the `getEnsureInitPointeeType` else-if (:166) which is null for a plain
   non-ensure_init local → branch body skipped, NO base-load check. **DISTINCT branch
   (Deref-first, not getFieldPath-None) — top candidate.**
3. `pp[i][j] = v` `[Index, Index]`, pp uninit LOCAL — Projections[0]==Index → getFieldPath
   None (not Field-first) AND not Deref-first. Base is a LOCAL pointer (not a field).
   The loaded base `pp` is a whole-local; checkOperand's whole-local path (:926) is the
   read-side check. Skipped on dest side. **Same skip-both-branches as F83 but base is a
   LOCAL not a struct field — possibly same root, different reachability. FOLD-candidate.**
4. `s.p[i].q = v` `[Field, Index, Field]` — same routing as cell 1. FOLD candidate.
5. `arr[i] = v`, arr uninit `_Owned/_Borrow _ArrayElem` LOCAL — `[Index]`. Projections[0]==Index
   → getFieldPath None, not Deref-first. Base `arr` is a LOCAL pointer. Same as cell 3 minus
   one Index. FOLD-candidate (local base).
6. `*(s.p + i) = v` pointer-arith — lowers to either `[Field, Deref]` (ptr-arith folded into
   address then Deref) or `[Field, Index]`. If `[Field, Deref]`: getFieldPath None (ends Deref),
   not Deref-FIRST (Field is first) → skips both → same F83 gap. If `[Field,Index]` == F83 exactly.
   FOLD-candidate either way unless it routes to markPointeeFullyInit.

**Ranked DISTINCT candidates** (only these can be a NEW filing — rest fold to F83's
getFieldPath-None miss):
1. `(*pp)[i]=v` — enters the **Deref-first branch** (:159), a DIFFERENT code path than F83.
   If the base `pp` load is unchecked here, the fix surface (Deref branch) differs from
   F83's (getFieldPath branch). DISTINCT-candidate. PROBE FIRST.
2. `*(s.p+i)=v` if it lowers to `[Field, Deref]` and hits markPointeeFullyInit at :165 —
   that would OVER-CREDIT (mark pointee init) like F55, a different mechanism. PROBE if cell 1
   inconclusive.

### R2 dest-projection cells — TRACED 2026-05-30 → ALL-FOLD into F83 (no distinct root)

Probed every reachable dest-Place shape against F83's transferStatement gap. Cell table
(ledger /tmp/probed_R2E2.md; probes /tmp/probe_R2E2_*.cbs):

| Dest shape | Place projections | Verdict | Why |
|---|---|---|---|
| `s.p[i]=v` (F83) | `[Field,Index]` | — | the filed exemplar |
| `s.arr[i].f=v` | `[Field,Index,Field]` | **FOLD-F83** | getFieldPathPrefix breaks at the Index (:501) → `Indices.size()!=Projections.size()` → None; not Deref-first. Field-after-index adds nothing. WRITE accepted / READ rejected / vg "Use of uninitialised value sz8". |
| `arr[i]=v`, `arr` uninit LOCAL `_Borrow _ArrayElem` | `[Index]` | **FOLD-F83** | getFieldPath None (not Field-first), not Deref-first. Base is a whole-LOCAL (vs F83's struct field) but the MISSING-branch is identical; one fix closes both. WRITE accepted / READ rejected / vg uninit-use sz8. |
| `(*pp)[i]=v` | `[Deref,Index]` | **SHAPE-REJECTED** | requires explicit `*` (forbidden in `_Safe`); double-`_Borrow` ptr-to-arrayelem also forbidden. Unreachable. |
| `pp[i][j]=v` | `[Index,Index]` | **SHAPE-REJECTED** | pointer-to-(`_Borrow _ArrayElem`) is forbidden (no nested borrow pointers). Unreachable. |
| `*(s.p+i)=v` | `*`-deref form | **CORRECTLY REJECTED** | the explicit `*` lowers the LHS base load through `checkOperand` → "use of uninitialized value: `s.p`". NOT a gap. (Pinpoints F83 to the `[ ]` subscript lowering specifically.) |
| `p->arr[i]=v`, `p` ensure_init param, `arr` `_Owned _ArrayElem` field | `[Deref,Field,Index]` | **SOUND** | enters Deref-first branch (:159) but routes to the ensure_init contract check, which correctly rejects "*p not initialized at return" (index write doesn't mark the `arr` field, per the documented array limitation). NOT a gap. |

**Conclusion**: every reachable false-negative dest shape collapses to the SAME root —
`transferStatement` Assign case (:94-174) has no branch for a dest Place that contains
an **Index** projection (getFieldPath returns None whenever an Index sits anywhere in the
chain; the Deref-first branch needs `Projections[0]==Deref`), so the loaded base pointer is
never fed into the read-side uninit-use check. One fix (route the Index-projection base load
through `checkOperand`) closes all cells. **No DISTINCT root → no new filing.** F83's
blast radius now mapped: `[Field,Index]`, `[Field,Index,Field]`, `[Index]`-on-local all fold.

## R3 source-side / aggregate-INIT mirror of F83 (2026-05-30) — ALL SOUND, no distinct root

**Hunt**: F83 is the dest-side `s.p[i]=v` Index write FN. Does the SOURCE/READ side
(checkOperand operand-iteration, :830-939), the aggregate/partial INIT side, or the
promote side (markFieldInit/tryPromoteParent :520-607) have an analogous tracking gap?

**Read-path is the SOUND MIRROR of F83 (write-only gap confirmed).** Every read /
address-of of an Index projection through an uninit pointer-or-array base feeds the
loaded base pointer into checkOperand and is rejected:
- `s.p[c]` read, s.p uninit → REJECT "use of uninitialized value: s.p".
- `o.in.p[c]` nested read → REJECT "o.in.p".
- `s.arr[c].q` read AND `sink(s.arr[c].q)` arg, s.arr uninit ptr-to-struct field
  (Place `[Field,Index,Field]`, the deepest reachable) → REJECT "s.arr".
- `&_Mut s.arr[c]`, s.arr uninit ptr field (AddressOf-of-Index) → REJECT "s.arr"
  (despite forEachRvalueOperand :947 skipping AddressOf — the base load is checked
  on a separate layer). 
The dest-side `transferStatement` Assign case (:94-174) is the ONLY place that drops
the base-load check for an Index-containing Place; the source side does not. F83 is
WRITE-side-exclusive.

**Aggregate/partial-INIT side is SOUND (C zero-fill, not a tracking event).**
- `struct S{int a; int*_Borrow _ArrayElem p;} s={.a=5}` (skip nonnull ptr field) →
  SHAPE-REJECTED at Sema ("type contains nonnull pointer must be properly initialized").
- `_Nullable p` skipped → zero-filled to null; read `s.p[c]` → "nullable cannot be
  dereferenced" (sound).
- `struct{int a;int b;} s={.a=5}; read s.b` → ACCEPTED, and vg --track-origins shows
  r=0 / 0 errors: C designated init zero-fills the omitted field to a DEFINED value.
  Accepting this read is SOUND, not an over-credit FN.

**Promote side (tryPromoteParent :536, getNumFields :441) is SOUND.**
- Anonymous nested struct `struct S{int tag; struct{int x;int y;};}`: init `tag` + `x`,
  leave `y` uninit, read `y` → REJECT "use of uninitialized value: s.y". getNumFields
  counts the anon-struct's members correctly; no over-promotion of the parent. Full-init
  control (`x` and `y` both set) reads clean.
- ancestor-init read laundering (:891-906) credits a sub-field read only when an ancestor
  is `==Initialized`, which only arises from a genuine whole-place write — sound.

**Verdict**: NO source/read/promote gap distinct from F83. F83 stays WRITE-side-exclusive.
Probes /tmp/explorer_probe.*; ledger /tmp/probed_R3E3.md.

## R4 terminator-operands — F88 continuation (2026-05-30) — CONFIRMED-new (pending file)

**Hunt**: F88 = `Call.Callee` operand never checked in `InitAnalysis::run`. Which OTHER
terminator-kind operands does the run-loop terminator-check (BSCIRInitAnalysis.cpp:1513-1543)
skip? Terminator kinds (BSCIR.h:774-782): Goto, SwitchInt, Call, Drop, Return, Unreachable.
The run loop has cases for ONLY `Terminator::Call` (:1515, checks `CD.Args`) and
`Terminator::Return` (:1525, checks return slot `LocalId{0}`). `transferTerminator` (:227)
also only matches `Call`. **`Terminator::SwitchInt`, `Goto`, `Drop`, `Unreachable` have NO
case anywhere in the init analysis.**

**THE GAP — `SwitchInt::Discriminant` operand is never fed to `checkOperand` → a `switch`
on an UNINITIALIZED value is accepted in pure `_Safe` (the byte-identical read is rejected).**
- Site: `InitAnalysis::run` terminator-check block (BSCIRInitAnalysis.cpp:1513-1543). No
  `if (T.K == Terminator::SwitchInt …) checkOperand(T.getSwitchInt().Discriminant, …)`.
  The discriminant (`SwitchIntData::Discriminant`, BSCIR.h:789) is produced by
  `lowerSwitchStmt`'s `lowerToOperand(SS->getCond())` (BSCIRBuilder.cpp:541); for a plain
  local or struct-field condition it is a direct `Copy` of the uninit Place — NOT pre-loaded
  through a statement-level `checkOperand`, so the read escapes entirely.
- **Exemplar = uninit struct FIELD** (cannot be dismissed as basic-type relaxation):
  `struct S{int tag;int other;} s; s.other=7; switch(s.tag){…}` → ACCEPTED; byte-identical
  `int y=s.tag;` → REJECTED "use of uninitialized value: `s.tag`". vg --track-origins:
  "Conditional jump or move depends on uninitialised value(s)" in `test`.
- Gap is GENERAL: plain `int x; switch(x)` is also accepted while `int y=x` is rejected
  (Control A). `switch` does NOT launder the discriminant (Control B: a later read of the
  same field is still rejected) — confirming the terminator just skips the use-check.
- `switch(*p)` (uninit borrow deref) is REJECTED — `*p` lowers through a load STATEMENT
  whose operand IS checked; only the DIRECT (field/local Copy) discriminant escapes.
- **DISTINCT from F88**: different terminator KIND. Call is at least partially handled
  (Args checked) and the fix adds one line to an existing case; SwitchInt has NO case at
  all (an entire unhandled terminator opcode). Different operand (Discriminant vs Callee).
- Defect class: C2 (terminator opcode-switch hole — the run-loop switch has no SwitchInt
  case) / C3 (a terminator operand never visited).
- Repro /tmp/F89_init_switch_discriminant_uninit_field.cbs; baseline
  /tmp/F89_baseline_uninit_field_read_rejected.cbs; ledger /tmp/probed_R4E3.md.
- Terminator×checked table: Goto(none), SwitchInt(Discriminant=**NO, this find**),
  Call.Args(YES), Call.Callee(NO=F88), Drop.Dropped(NO — owned-only, ownership-analyzer
  normally guards; separate surface, unprobed), Return slot(YES), Unreachable(none).

## chain-Z comma/arith launder (vs F91/F92/F93) — PROBED-SOUND 2026-06-04

The init analysis is IMMUNE to the F91/F92/F93 comma/pointer-arith blind spot because it runs on lowered
BSCIR, not the AST. `int y = (0, x)` and `int *_Borrow _ArrayElem q = p + 0` (x/p uninit) both correctly
report "use of uninitialized value" — BSCIRBuilder.lowerExpr flattens comma + pointer arith into direct
operand reads (checkOperand sees them). Confirms chain Z is bounded to AST-walking analyzers; the
IR-lowering step is the firewall. (Distinct from F88/F90 which ARE IR-level holes — missing
terminator/operand KINDS, not expr-shape laundering.)

## meetStates / merge / entryState (BSCIRInitAnalysis.cpp:329 / 337 / 48) — confluence PROBED-SOUND 2026-06-04

**Invariant**: at a CFG join, a local/field initialized on only SOME predecessors must become MaybeInit
(reading it → "use of possibly uninitialized value"). meetStates: A==B→A else→MaybeInit (intersection).

**Peers**: entryState (pre-populates EVERY local in LocalStates → no "missing local" at joins), transferStatement
(per-stmt init), the read-check (situations consuming InitState). F26/F75 are the analogous merge holes in
OTHER analyzers (DPVD / owned-field) — this is the init lattice's confluence.

**Why the LocalStates vs FieldStates merge asymmetry is SOUND**: merge() handles missing entries with
meet-against-Uninitialized for FieldStates (both directions, :356-381) but NOT for LocalStates (:341-353,
Src-only copy, no Dst-not-in-Src loop). Safe because entryState pre-populates all locals (return slot,
params, every local) → LocalStates keys are identical across all predecessors, so "missing local" never
occurs. FieldStates ARE lazy (added on first field-init) → genuinely need the missing-entry meet.

**Candidates**:
1. Conditional partial init (whole-local `if(c){x=1;} return x;` + field `if(c){s.b=2;} return s.b;`) —
   **PROBED-SOUND**: both error "use of possibly uninitialized value" (MaybeInit produced + read-checked).
2. Array-field element init (:107 `break` — "array fields cannot be init'd element-by-element"): `arr[i]=x`
   is NOT tracked; whole-array init via `{}`/__assume_initialized required. Does reading `arr[i]` after a
   partial element-write get caught (whole-array still Uninit), and is there a read-side hole? UNPROBED.
3. EnsureInitDerefStates merge (:384, the `__attribute__((ensure_init))` *param deref state) — merged with
   meetStates like the others; the ensure_init×_Nonnull pointee composition is under audit elsewhere. UNPROBED.

## array-element init (transferStatement :104-108 break) — PROBED-SOUND (conservative) 2026-06-04

**Invariant**: an array must be whole-initialized (`{}`/`__assume_initialized`) before any read; element
writes `a[i]=x` are NOT tracked, so the array stays Uninitialized until whole-init.

**Behavior (probed)**: `int a[3]; a[0]=1; return a[2];` → rejected "use of uninitialized value: `a`";
`a[0]=1;a[1]=2;a[2]=3; return a[0];` → STILL rejected (element writes never mark init); `int a[3]={0}; return a[2];`
→ ACCEPTED; bare `return a[0];` → rejected. So: conservative, no soundness FN. The "all-elements-written
still rejected" is a documented FALSE POSITIVE (design: arrays need `{}`), not fileable.

**Why no FN**: element writes hit the `break` (:107 for array FIELD paths) / Index-projection skip (F83 for
local-array element writes) → no state change → array stays Uninit → reads conservatively rejected. The only
state that marks an array Init is whole-init (`{}`) or `__assume_initialized` (programmer assertion).

**Candidates**:
1. partial/whole element-write then read — **PROBED-SOUND** (conservative over-strict, by design).
2. `__assume_initialized` on an array then read uninit element — assertion-based, should be accepted (trust);
   the FN risk is if `__assume_initialized` is too coarse vs a later real use. UNPROBED.
3. out-param / ensure_init: passing `&a` (uninit array) to a callee with ensure_init — does the array get
   marked Init at the call, and is the callee actually forced to init it? UNPROBED (ties to
   composition_init_null.md ensure_init audit).

## init-analysis field-tracking depth (limit-sweep, F96 sibling-analyzer) — probing
**Invariant**: reading an UNINITIALIZED struct field at any (finite) nesting depth
must be flagged "use of uninitialized value"; field-init tracking must not have a
silent depth cap (cf. F96 = the OWNERSHIP analyzer's depth=10 cap → silent leak).
**Peers**: F96 (ownership initS depth=10 cap), getFieldPath, CheckInit, F19/F44/F45
(field-init tracking). DIFFERENT analyzer (BSCIRInitAnalysis vs BSCOwnership).
**Candidates**:
1. **deep uninit field read past a cap → missed (FN) — probing (limit-sweep)**.
2. deep partially-init (init some, read other) depth cap. UNPROBED.
3. array-of-struct field init depth. UNPROBED.

## union cross-variant init: write-small-read-large tail (TransferFunctions :174-212) — probing
**Invariant**: writing a union member initializes bytes for THAT member; a
cross-variant read of a member extending BEYOND the written member's size reads
uninit tail bytes and must be flagged.
**BUG CANDIDATE**: :180-188 — writing ANY union member (even a small `u.a`) marks
the WHOLE local Initialized ("so cross-variant reads pass"). If the read member is
LARGER (e.g. `u.big.z` beyond `u.a`'s 4 bytes), the tail is uninit but the
whole-local-Init heuristic passes it → uninit-read FN.
**Peers**: F42 (union alias), F78 (union nullability), checkOperand.
**Candidates**:
1. **write small `u.a`, read larger `u.big.z` (tail uninit) → FN? — probing** (HIGH if real).
2. write small, read large via valgrind runtime confirm. UNPROBED.
3. nested-union cross-variant (UnionDepth>0). UNPROBED.

### RESOLUTION (2026-06-22 explorer) — CONFIRMED-new (pending file) via the WHOLE-UNION-READ reach path

The 2026-06-08 session marked this candidate SHAPE-REJECTED because it read the
larger variant **directly** (`u.b` field read → needs `_Unsafe` → blocked, and
`_Unsafe` suppresses the init diag). **That closed only ONE reach path.** The
larger-than-written read is ALSO reachable through **whole-union operations that
need NO `_Unsafe`**: whole-union COPY (`union U v = u;`), RETURN (`return u;`),
and pass-by-value (`sink(u)`). All three lower to a whole-local read that
`checkOperand` checks in PLAIN `_Safe` (verified: a never-written whole-union
copy IS rejected "use of uninitialized value: u").

**THE FN**: transferStatement union-struct/variant branch (:180-187) marks the
WHOLE union local `Initialized` after writing ONE variant ("so cross-variant
reads pass"). For a variant SMALLER than the union (`int a` in `union{int a;
long b;}`), the tail 4 bytes stay uninit, but the whole-local-Init flag makes
every later whole-union read pass. Only the small FIELD write `u.a` needs
`_Unsafe` (the union-field-access gate); the laundered whole-union COPY/RETURN is
plain `_Safe`.

**Asymmetry (clean 3-way)**:
- no write → `union U v = u;` REJECTED "use of uninitialized value: u" (analysis checks).
- write small `u.a` (4B) → COPY/RETURN ACCEPTED (FN; tail 4B uninit).
- write full `u.b` (8B) → ACCEPTED (correct).
Runtime: valgrind "Conditional jump or move depends on uninitialised value(s)"
when the caller branches on `r.b`.

**Distinct from**: F42 (borrow union alias), F75 (OWNERSHIP merge union-the-set,
different analyzer), F78 (nullability default-init union FP, opposite direction),
F80 (decl-time owned union classification). This is the INIT-analysis
whole-local-Init over-promotion on a partial (smaller-than-union) variant write.
Defect class C1/C6 (the whole-local-Init heuristic ignores the byte-size
mismatch between the written variant and the read width).

Repro: /tmp/F_union_write_small_read_whole_tail_FN.cbs ; baseline
/tmp/F_union_write_small_read_whole_tail_BASELINE.cbs. Severity MEDIUM (the
trigger requires `_Unsafe` for the variant write, but the LAUNDERED read is in
plain `_Safe` and the symptom is genuine uninit-byte UB).

## ensure_init_if_ret caller-side propagation (transferStatement Assign :254-371 + transferTerminator Call/SwitchInt :421-672) — PROBED-SOUND 2026-06-08

**Feature**: `int *_Borrow __attribute__((ensure_init_if_ret(N))) out` contract — when
the callee returns int/_Bool literal N, `*out` is init on that path. CALLER side: after
`r = f(&_Mut x)`, the analyzer credits `x` init inside the branch that proves `r == N`.

**Mechanism (read in full)**:
- `transferStatement` Assign (:254-371): on `r = f(&x)` the Call terminator records a
  `PendingCondInit{OutParamLocal=x, RetLocal=r, CondValue=N}` (terminator :501-547). On a
  later `dst = (r == K)` BinaryOp it records a `ComparisonFact{ComparedLocal=r,
  ComparedValue=K, IsEq}` keyed by `dst` (:322-345). Copy-propagation of both PCIs and
  ComparisonFacts through `dst = src` (:350-369). `KnownConstants` tracks const temps so
  `-1` etc. fold (:295-320). `invalidateLocal` (:261-274) drops facts when the holding
  local is reassigned / address-taken / mutably-borrowed.
- `transferTerminator` SwitchInt (:588-672): looks up the discriminant's ComparisonFact,
  classifies the edge true/false (`classifyEdge` :605-627, handles both bool lowerings),
  and credits init ONLY on `(IsEq && IsTrueEdge) || (!IsEq && IsFalseEdge)` AND
  `PCI.RetLocal==CompLocal && PCI.CondValue==CompValue` (:638-665). Delegation credit when
  `OutParamLocal` is itself an ensure_init_if_ret param (:644-650).
- `checkEnsureInitIfRetAtReturn` (:1608-1697): callee verifier; delegation credit requires
  `PCI.CondValue==CondValue && PCI.RetLocal==RV.SourceLocal` (:1653-1660).
- Sema variance: `CheckEnsureInitFunctionPointerType` (SemaBSCOwnership.cpp:927-988),
  invoked ONLY from SemaExpr.cpp:10802 inside `CheckAssignmentConstraints`.

**Invariant**: caller credits `*out`/`x` init iff the branch PROVES `ret == N` (one of the
4 forms e==N/N==e/e!=N/N!=e, or a copy/bool-store thereof), the addr-of arg genuinely
denotes the credited place, and the fnptr contract is preserved across every assignment.

**Probed SOUND (14 probes, no FN) — every ranked briefing direction**:
1. bool-stored compare `int a=(r==0); if(a)` → CLEAN (richer-than-4-forms; SOUND, a==(r==0)).
2. compound `&&`/`||` (`if(r==0 && c)`, `if(r==0 || c)`) → both conservatively REJECTED
   "possibly uninitialized" (the || true-edge correctly does NOT prove r==0; SOUND).
3. inverted bool `int a=(r==0); a=!a; if(a)` → REJECTED (invalidateLocal drops the fact on
   reassign of `a`; SOUND — `a` no longer == `(r==0)`).
4. comma addr-of `try_init((0, &_Mut x))` → Sema WARNS "effect cannot be verified" but the
   dataflow STILL credits x on r==0 → CLEAN. NOT an FN: the borrow genuinely reaches x
   (verified: comma-addresses-z-read-x correctly errors on x; no-if comma errors on x), so
   crediting x under the callee contract is sound. The warning is spuriously conservative
   (Sema vs dataflow disagree on recognizing the addr-of, but dataflow's credit is the
   sound answer). Benign warning/dataflow inconsistency, NOT fileable.
5. paren addr-of `(&_Mut x)` → CLEAN (correct).
6. ternary-selected addr-of `c ? &_Mut x : &_Mut z` → REJECTED at the `&_Mut` site (borrow
   of uninit in a ternary); no over-credit.
7. fnptr-variance via struct-field init / array-element init / return-stmt of a plain fn
   into a contract-fnptr type → ALL three correctly ERROR (the variance check routes through
   CheckAssignmentConstraints for aggregate-element and return conversions too). No bypass.
8. delegation cond-value mismatch: inner `ret(0)`, guard `if(r==5) return 0;` → correctly
   ERRORS "*out not initialized" (r==5 does not prove inner init'd *out); baseline `if(r==0)`
   clean. The guard value must match the inner contract's cond. SOUND.

**Conclusion**: the caller-side propagation, the SwitchInt edge classification, the
comparison-fact value/eq matching, the addr-of arg decoding, the fnptr variance, and the
delegation cond-value composition are all SOUND. The code carries explicit soundness
reasoning at every step (the "Marking unconditionally is unsound" comment at :502-505 etc.)
and the regression corpus is dense. No new root cause from this slice. Frontier hint for a
future session: the union write-small-read-large tail (candidate above, :501) and the
EnsureInitDerefStates×_Nonnull pointee composition (composition_init_null.md) remain the
open in-scope surfaces; the ensure_init_if_ret caller-side logic is now CLOSED.

## ensure_init_if_ret path-count verification (boundary-sweep on new feature) — probing
**Invariant**: the "*out initialized on ALL paths returning N" check must hold
regardless of branch count; a path-count cap would miss an uninit path past the
limit (FN — caller reads uninit). cf. ownership depth=10 cap (F96).
**Peers**: checkEnsureInitIfRetAtReturn (:1608), F96 (depth cap), explorer audit (sound on forms).
**Candidates**:
1. **N conditional inits of *out then `return 0`; all-false path leaves *out uninit → "may not be init on all paths" fires at all N? — sweeping**.

## ensure_init_if_ret unconditional-use (caller core contract) — probing
**Invariant**: after `try_init(&_Mut x)` (ensure_init_if_ret(0)), x is init ONLY
on the ret==0 branch; using x WITHOUT gating on the return must be REJECTED (x may
be uninit if the call returned non-0). Over-crediting = HIGH FN (caller reads uninit).
**Peers**: explorer audit (forms sound), caller-field test, checkEnsureInitIfRetAtReturn.
**Candidates**:
1. **use x with NO ret-check → REJECT? — probing** (FN if accepted).
2. use x after `if (r != 0) return;` (early-return inversion). UNPROBED.
3. use x after checking the WRONG value `if (r == 1)`. UNPROBED.

## ensure_init_if_ret negative/edge cond value — probing (wide-return sibling)
**Invariant**: the contract cond value N (integer literal) and the caller's `ret==N`
comparison must match by full value incl. SIGN; a negative N must credit only on
ret==N, not on a sign-confused value. (wide-return fixed 32-bit truncation.)
**Candidates**:
1. **`ensure_init_if_ret(-1)`: credit on `ret==-1` only, NOT on `ret==0` — probing**.
2. large N near INT_MAX. UNPROBED.
3. unsigned-comparison mismatch. UNPROBED.

## ensure_init_if_ret runtime soundness (capstone) — probing
**Invariant**: a compile-time-ACCEPTED caller (reads *out only in the ret==N branch)
must be runtime-clean (no uninit read) on ALL execution paths — compile-time
soundness must match runtime.
**Candidates**: 1. verified callee + gated caller, valgrind both paths → clean?

## array element init tracking — probing
**Invariant**: arrays must be fully init (via `{}` / `__assume_initialized`), NOT
element-by-element (:170-172); reading an element of a not-fully-init array must
be caught — a single element write must NOT mark the whole array init (else FN).
**Candidates**:
1. **read arr[1] after only `arr[0]=1` → caught? — probing** (FN if one write marks array init).
2. fully-init `int arr[3]={0}` then read → ACCEPT (control).

## ensure_init_if_ret × array-element out-param — probing
**Invariant**: `try_init(&_Mut arr[0])` (ensure_init_if_ret(0)) credits arr[0] on
ret==0; reading a DIFFERENT element arr[1] (not passed) must still be uninit (FN if
the contract wrongly credits the whole array). Interacts with all-or-nothing array rule.
**Candidates**:
1. **read arr[1] after `try_init(&_Mut arr[0])==0` → REJECT (uninit)? — probing** (FN if accepted).
2. read arr[0] (the passed element) → behavior?

## ensure_init_if_ret cond value vs return-type range — probing
**Invariant**: a cond value outside the return type's representable range (e.g. 2
for `_Bool`) makes the contract unsatisfiable; should be diagnosed or handled
(caller `if(ret==2)` is dead, so vacuously no uninit read — sound either way).
**Candidates**: 1. `_Bool` return + `ensure_init_if_ret(2)` → diagnostic or accepted?

## __assume_initialized escape — probing
**Invariant**: `__assume_initialized(&x)` asserts x is init (deliberate escape);
reading x after is accepted. Misuse is the user's responsibility (like _Unsafe).
**Peers**: F58 (side-effecting arg dropped — fixed), ensure_init_if_ret.
**Candidates**: 1. `__assume_initialized(&x)` then read x → accepted (escape works)?

## __assume_initialized field granularity — probing
**Invariant**: `__assume_initialized(&s.a)` assumes ONLY s.a init; reading a
DIFFERENT field s.b (not assumed) must still be uninit (FN if whole struct assumed).
**Candidates**: 1. assume s.a, read s.b → REJECT (uninit)?  2. read s.a → ACCEPT.

## field-by-field struct init then whole-struct use (FP hunt) — probing
**Invariant**: a struct with ALL fields individually initialized must be usable as a
whole (return / pass-by-value); requiring {}-init would be an FP. (Contrast arrays:
all-or-nothing.) Partial field init then whole use must still REJECT (soundness).
**Candidates**: 1. `s.a=1; s.b=2; return s;` → ACCEPT (no FP)?  2. only s.a set, return s → REJECT.

## array fully written element-wise then read (all-or-nothing FP) — probing
**Invariant**: writing ALL elements `arr[0..n-1]=..` then reading — if rejected,
it's a canonical-idiom FP (whole array initialized element-wise but unreadable).
Documented rule says arrays need {}-init; this measures how sharp that FP is.
**Candidates**: 1. write all 3 elems then read arr[0] → ACCEPT or FP-REJECT?

## init-analysis nested-struct depth vs documented rule 8 (spec-conformance) — probing
**Invariant**: USER MANUAL §3.7.3 rule 8 — nested-struct field tracking is "任意深度"
(ARBITRARY depth); all-leaves-init auto-promotes the whole struct. A depth cap in
the INIT analysis would deviate: FP (deep all-init wrongly rejected) or FN (deep
uninit leaf wrongly accepted). cf. F96 = OWNERSHIP analysis depth=10 cap (separate pass).
**Candidates**:
1. **depth-15 nested, leaf init, use whole struct → ACCEPT (rule 8)? — probing** (FP if rejected).
2. depth-15 nested, leaf UNINIT, use whole struct → REJECT? (FN if accepted).

## ensure_init no-alias-before-init (§3.7.4 conformance) — probing
**Invariant**: USER MANUAL §3.7.4 — an `ensure_init` param cannot be reassigned or
aliased before `*param` is initialized (documented errors). Test conformance + a
variant not in the docs (pass `out` to another fn before init = alias-launder?).
**Candidates**:
1. **alias `int*_Borrow p = out;` before `*out` init → REJECT (documented)? — probing**.
2. pass out to another fn before init → REJECT or FN-gap?

## ensure_init_if_ret fnptr arg-MISMATCH (rule 5 conformance) — probing
**Invariant**: USER MANUAL §3.7.5 rule 5 — when BOTH fnptr types carry
ensure_init_if_ret, the `arg` must be IDENTICAL; assigning `(A)` to a slot
expecting `(B)`, A≠B, must be REJECTED. Laundering A≠B = FN (caller credits init
on the wrong return value). Explorer tested has-vs-not-has, NOT arg-mismatch.
**Candidates**:
1. **FP(arg=0) g = fn_with_arg_2 → REJECT? — probing** (FN if accepted).
2. redecl arg-mismatch (rule 6) → REJECT? UNPROBED.

## ensure_init_if_ret TRANSITIVE delegation (callee-side verify) — probing
**Invariant**: a callee `outer` with ensure_init_if_ret(0) that inits *out by
delegating to an inner ensure_init_if_ret(0) fn (crediting *out on the inner's
ret==0 path) must PASS the callee-side verification — the inner contract must
propagate. Else FP (valid delegation rejected).
**Candidates**: 1. outer delegates to inner, returns 0 after if(r==0) → verify passes (no FP)?

## ensure_init_if_ret on an _Owned out-param (init×ownership) — probing
**Invariant**: if the contract inits `*out` to an `int *_Owned`, after the ret==arg
branch the caller OWNS *out and must free it; not freeing = leak (must be caught).
Novel init×ownership interaction.
**Candidates**: 1. SHAPE-REJECTED — owned out-param TARGET isn't the feature's use case (out-param is plain data; the feature's owned tests use a separate by-value owned param). Caller `&_Mut` of uninit owned correctly blocked by ownership move-check, not the init exemption.

## ensure_init_if_ret multi-param, per-param different arg — probing
**Invariant**: with params a(ret==0) and b(ret==1), the `if(ret==0)` branch credits
ONLY a, not b (b needs ret==1). Reading b on the ret==0 branch must REJECT (FN if credited).
**Candidates**: 1. read a (ok) + read b (uninit) on ret==0 branch → b rejected?

## ensure_init_if_ret comparison-value invalidation — probing
**Invariant**: if the return-value var `r` is REASSIGNED after the call, `if(r==arg)`
no longer reflects the actual return → the param must NOT be credited (FN if credited).
**Candidates**: 1. `r=f(&a); r=0; if(r==0){use a}` → a NOT credited (reject)?

## ensure_init_if_ret callee re-points out before init (ptr-aliased) — probing
**Invariant**: if the callee REASSIGNS `out` to another location before init, `*out=..`
inits the NEW target, not the original; returning arg must be REJECTED (original *out
never init). FN if the re-pointed write credits the original.
**Candidates**: 1. `out=other; *out=1; return 0;` → at-return error (original *out uninit)?

## ensure_init_if_ret unsupported || comparison form (soundness) — probing
**Invariant**: `if(r==0 || c){use a}` — branch reachable via c with r!=0 (a uninit);
a must NOT be credited. A naive "I saw r==0 in the condition" credit = FN.
**Candidates**: 1. `if(r==0 || c){use a}` → a rejected (sound conservative) or credited (FN)?
2. `if(r==0 && c){use a}` → a rejected (documented: && unsupported) — conservative FP.

## ensure_init_if_ret cross-call return-value association (soundness) — probing
**Invariant**: each credited var associates with ITS OWN call's return; `if(r1==0)`
credits a (passed to the r1 call), NOT b (passed to the r2 call). Crediting b = FN
(wrong return-value associated across two calls).
**Candidates**: 1. `r1=f(&a); r2=f(&b); if(r1==0){use a; use b}` → b rejected (FN if credited)?

## ensure_init_if_ret callee return-via-variable (soundness) — probing
**Invariant**: callee-side verify must track the returned VARIABLE's value; `result=0;
if(cond)*out=1; return result;` returns 0 always but inits *out only if cond → must
REJECT (FN if the verify can't follow the return through a variable).
**Candidates**: 1. return-via-var, conditional init → at-return error?

## ensure_init_if_ret credit through fnptr indirect call — probing
**Invariant**: calling through a fnptr whose TYPE carries ensure_init_if_ret(0) must
credit *arg on the ret==0 branch (same as direct). Missing = FP; unconditional = FN.
**Candidates**: 1. `fp(&_Mut a); if(r==0){use a}` → credited (works via fnptr)? + unconditional use → reject?

## init-analysis Rvalue operand-check completeness (C3) — PROBED-complete
**Invariant**: transferStatement's Rvalue switch (BSCIRInitAnalysis.cpp:1988-2028) must
check operands for every Rvalue kind. VERIFIED COMPLETE: Rvalue::Kind enum (BSCIR.h:435)
= exactly 10 kinds {Use,Ref,AddressOf,BinaryOp,UnaryOp,Aggregate,Array,Cast,NullPtr,SizeOf};
all 10 handled (NullPtr/SizeOf correctly no-op — unevaluated/no operand read). No
fall-through FN. Dest Deref-projection prefixes also checked (2030-2045). Known gaps are
ELSEWHERE: F83 (dest Index projection), F88 (Call callee operand) — both filed.

## ensure_init_if_ret callee goto skips init (soundness) — probing
**Invariant**: a callee `goto` that skips `*out=..` before `return arg` must be caught
(*out uninit on the goto path). The at-return check must inspect goto-reachable paths.
**Candidates**: 1. `if(c) goto done; *out=1; done: return 0;` → reject (c-path skips init)?

## ensure_init_if_ret return-var incremented (r++) before compare — probing
**Invariant**: `r++` mutates the return-value var; `if(r==0)` after `r++` means
`return==-1` (≠contract 0) → a must NOT be credited. Naive `r==0` credit = FN.
**Candidates**: 1. `r=f(&a); r++; if(r==0){use a}` → reject (r no longer == return)?

## ensure_init_if_ret bare-truthiness if(r) form — probing
**Invariant**: `if(r) return; use a;` — bare `if(r)` ≡ `if(r!=0)`; fall-through is r==0
→ a credited (for ensure_init_if_ret(0)). Recognizer handling bare truthiness = precise;
literal-==-only = conservative FP.
**RESOLVED**: bare if(r)/if(!ok) DELIBERATELY unsupported (code comment :669-672 names them; four-forms by design). NOT a bug. The _Bool variant is the painful case but still intended.

## ensure_init_if_ret value-vs-contract mismatch in != form (SOUNDNESS) — probing
**Invariant**: for ensure_init_if_ret(1), `if(r != 0) return; use a;` — fall-through is
r==0, NOT the contract value 1 → *out uninit → must REJECT. If the recognizer credits on
the form's literal (0) ignoring the contract value (1) → FN (HIGH).
**Candidates**: 1. `if(r != 0) return; use a;` w/ contract(1) → reject (ties to contract val)?

## ensure_init_if_ret retry pattern (same var, two gated calls) — probing
**Invariant**: `r1=f(&a); if(r1!=0){ r2=f(&a); if(r2!=0)return; use a; } else { use a; }` —
both uses gated by their own call's success → both credited, no uninit path. FP if either
rejected; FN if an uninit path slips.
**Candidates**: 1. retry pattern → both uses accepted, no FN?

## ensure_init_if_ret INT_MAX/INT_MIN contract value (32-bit boundary) — probing
**Invariant**: contract value at the 32-bit signed boundary (INT_MAX 2147483647,
INT_MIN -2147483648) must match exactly (credit on r==boundary, not on off-by-one).
**Candidates**: 1. INT_MAX credit on r==INT_MAX, reject on r==INT_MAX-1.

## ensure_init_if_ret multi-iteration runtime failure-path — probing
**Invariant**: a gated caller (`if(r==0){read v}`) driven through many success+failure
calls must never read uninit v at runtime (valgrind clean — no uninitialised-read).
**Candidates**: 1. alternating ok/fail ×100 under valgrind → 0 uninit reads?

## ensure_init_if_ret recursive callee (self-delegation) — probing
**Invariant**: a callee with ensure_init_if_ret(0) that `return f(out, n-1)` delegates
*out's init to its OWN contract (modular: recursive call uses f's signature). Base case
inits *out. Verify must accept (transitive delegation to self).
**Candidates**: 1. recursive f, base inits *out, recursive returns f(out,n-1) → verify accepts?

## ensure_init_if_ret callee reads *out before init (soundness) — probing
**Invariant**: the callee must not READ `*out` before writing it — the caller passed
a (possibly) uninit location; reading it is an uninit read → must be caught.
**Candidates**: 1. `int tmp = *out; *out = 1; return 0;` → reject (reads uninit *out)?

## ensure_init_if_ret + _Nonnull on same out-param (composition) — probing
**Invariant**: `int *_Nonnull __attribute__((ensure_init_if_ret(0))) out` — nullability
+ init contract on the same param should compose (both enforced).
**Candidates**: 1. _Nonnull + contract: compiles + contract still credits/rejects?

## switch fall-through init modeling — probing
**Invariant**: `case 1: a=5; /*no break*/ case 2: use a;` — entering at case 2 directly
leaves a uninit; the analysis must flag maybe-uninit (FN if it assumes case-1 fall-through).
**Candidates**: 1. fall-through use → maybe-uninit flagged?

## init analysis: goto skips an initializer — probing
**Invariant**: `goto skip; int a=5; skip: use a;` — the goto jumps over the init, so a
is uninit at the use; must flag (FN if the skipped init is assumed to run).
**Candidates**: 1. goto-over-init then use → uninit flagged?

## analyzeReturnValue (ensure_init_if_ret return extraction, :1545-1606) — read, SOUND
Extracts the callee's return value for contract verification. Folds constants
(foldConstOperand), one-level copy-prop trace (Use/Cast/UnaryOp), conservative join
(>1 pred → unknown), Visited-set cycle guard. Multi-level callee return-copy chains
stay conservative (requires *out init — FP-leaning, no FN). No defect found.

## markPointeeFullyInit (:1017) + markAllFieldsInit (:1034) — read, SOUND
markPointeeFullyInit: marks Base's deref-state init (if tracked) + all pointee struct
fields. markAllFieldsInit: recurses nested struct/union field paths (union = all variants,
per rule 7; array field = marked at field level, not elements, per rule 6). No defect.
The F99 gap is upstream (:224 never calls these for `*b.p`); the mutators themselves are fine.

## ensure_init_if_ret contract laundered via fnptr CAST (rule-5 bypass) — probing
**Invariant**: assigning a no-contract fn to a contract-fnptr is rejected (rule 5). An
explicit `(FP0)bar` cast must ALSO be rejected (or not credit), else the caller credits
*out per the contract while the callee never inits it → FN (caller reads uninit).
**Candidates**: 1. `FP0 g = (FP0)bar;` (bar no contract) → rejected? then call+credit → FN if laundered.

## meetStates (:680) + merge (:688) — read + probed, SOUND (C5 home for init)
meetStates: A==B→A; any disagreement (Init/Uninit/MaybeInit mix)→MaybeInit (→ reads rejected = sound).
merge: LocalStates — meet for common keys; Src-only key → take Src value (NO meet-w-Uninit); no Dst-only handling.
FieldStates — SYMMETRIC: Src-only→meet(Src,Uninit) (:711); Dst-only→meet(Uninit,Dst) (:723). Conservative.
SOUNDNESS: confirmed — `int x; if(c){x=1;} return x` → MaybeInit "possibly uninitialized" (locals seeded
at declaration as Uninit → present on both paths → meet applies). The missing-Local case is block-scoped
locals not live after the join → safe.
**LATENT FRAGILITY (not a bug now):** LocalStates merge lacks the symmetric Dst-only handling FieldStates
has — relies on the invariant "every live local is in both LocalStates (seeded at decl)". If local-tracking
ever became lazy/assignment-only, a local Init-on-Src/absent-on-Dst would be taken as Init → FN. FieldStates
already guards this explicitly; LocalStates does not. Worth a defensive symmetric guard. Flag for maintainers. TRIGGER ATTEMPT 2026-06-08: `if(c)goto L; int x=5; L: return x;` (goto skips x's init — the most likely way to get a local absent-on-one-path) → CAUGHT 'possibly uninitialized: x'. So x is seeded on the goto path too → merge applies → fragility UNREACHABLE. Confirmed latent/safe (not a bug); guard is defensive hardening only.
BOTH C5 merge homes (init meetStates + nullability mergeVD) now validated SOUND.

## ComparisonFact creation+propagation (:322-369) — read, SOUND (ensure_init_if_ret caller-credit core)
INVARIANT: for `dst = (a OP b)` where OP ∈ {BO_EQ, BO_NE} (op-gated at :324 — `<`/`>`/etc. EXCLUDED,
no misfire), tryExtract pulls (local, const-literal) in BOTH operand orders (:337-338 = `e==v` AND `v==e`),
records ComparisonFact{ComparedLocal, ComparedValue, IsEq=(Op==BO_EQ)}. The 4 doc-supported forms
(e==v, v==e, e!=v, v!=e) = {2 orders} × {IsEq true/false}. Propagated through `dst=src` copies (:362-368),
with explicit rehash-safety (copy Fact out before insert, :364-367 — avoids use-after-free on map rehash).
KnownConstants tracked separately (:308-319) for const/cast/unary folding. Matches behavioral validation
(49-angle exhaustive: all 4 forms credit, <,> don't, copy-prop works, value range [-32768,32767]). No gap.
ENSURE_INIT_IF_RET CORE fully read: comparison-facts (caller-credit) + analyzeReturnValue (callee return)
+ checkEnsureInitDerefReads (callee *out read) + the rule-5 fnptr check (F100 cast-bypass). Sound except F100.

## PendingCondInit machinery (:482-516) — read + probed, SOUND (caller-credit deferred *out)
INVARIANT: at a call with an ensure_init_if_ret param, record a PendingCondInit{RetLocal=dest r,
OutParamLocal=&x/&x.field from ArgPlaces[I]} rather than marking *out init — DEFERRED to the matching
SwitchInt edge (if r==N) or return. Marking unconditionally would be UNSOUND (:502-505: `r=inner(out);
return 0` does NOT establish *out — out is init only if inner returned the cond value, not on the `return 0`).
Multi-arg safety (:482-484): invalidate dest PCIs EXACTLY ONCE before adding this call's PCIs (so a
multi-out-param call doesn't drop its own sibling PCIs). PCIs erased when RetLocal/OutParamLocal overwritten
(:262), propagated through copies (:354).
PROBED: `int r=inner(out2); return 0;` (unconditional return) → "'*out2' not initialized at return" (CAUGHT,
FN avoided); `return inner(out2);` (proper delegation) → ACCEPTED. Implementation matches the comment's soundness.
ENSURE_INIT_IF_RET MACHINERY now FULLY read: ComparisonFacts + PendingCondInits + analyzeReturnValue +
checkEnsureInit{AtReturn,DerefReads} + rule-5 fnptr check. Sound except F100 (cast-launder).

## checkEnsureInitAtReturn (:1511) — read, SOUND (ensure_init unconditional callee-verify)
INVARIANT: at each return, for every ensure_init param (skips ensure_init_if_ret, getIfRetCondValue),
if its EnsureInitDerefState is Uninitialized OR MaybeInit → diagnose (NotInit/MaybeNotInit, or Reassigned
if re-pointed). So unconditional contract requires *out FULLY Init on every return path — matches behavioral
(conditional/loop-cond init → MaybeInit → caught; all-paths → Init → accepted). ensure_init_if_ret verified
separately (checkEnsureInitIfRetAtReturn:1608, per-path against the cond value). Clean split, no gap.
ENSURE_INIT/IF_RET callee-verify FULLY read now: both contracts' return-verification sound.

## SwitchInt edge PCI resolution (:588-672) — read, SOUND (caller-credit completion)
INVARIANT: at a SwitchInt(if) terminator, for each PendingCondInit matching the discriminant's
ComparisonFact (RetLocal==CompLocal && CondValue==CompValue), credit *out ON the edge where
`CompLocal==CompValue` is proven: `(IsEq && IsTrueEdge) || (!IsEq && IsFalseEdge)` (:643) — EQ-true
or NE-false. classifyEdge (:605-627) computes true/false PER-TARGET (handles both BSCIR bool encodings
[0:false,..]/[1:true,..] + the Otherwise edge via ZeroInList). Credit has 3 shapes: delegation
(OutParam is itself ensure_init_if_ret → EnsureInitDerefStates Initialized), direct (LocalStates +
markAllFieldsInit), field (markFieldInit on FieldPath). Bare `if(ok)`/`if(!ok)` (no ComparisonFact)
→ NO credit (:669-672, intended — not one of the 4 spec forms; this is the F97-adjacent _Bool case).
Matches all behavioral results (4 forms credit, bare-if doesn't, delegation/field/struct work).
**ENSURE_INIT_IF_RET MACHINERY READ END-TO-END**: ComparisonFact create (:322) → PCI create@call (:496)
→ SwitchInt edge credit (:588) → analyzeReturnValue (:1545) → checkEnsureInit{,IfRet}AtReturn (:1511/:1608)
→ checkEnsureInitDerefReads (:1455) → rule-5 fnptr check (SemaBSCOwnership:953). ALL SOUND except F100 (cast bypasses rule-5).

## ensure_init_if_ret FIELD-TRACKING read-expansion (2026-06-08, per user "not saturated")
- markFieldInit (:913) → tryPromoteParent (:929): promotes parent to Init when ALL siblings Init
  (getFieldInitState per sibling, :970-977), recursing up; at top-level (Parent.Indices empty) the
  target is EnsureInitDerefStates[Base] (the deref state, :982-988) — THIS is how field-by-field init
  aggregates to the *out deref state the verify checks. Union special case (:941-965): any variant
  promotes the parent (rule 7) + clearUnionFieldEntries. getNumFields (:834) counts struct fields,
  0 for union. Aggregation SOUND (probed: nested struct all/partial, whole-write).
- getEnsureInitPointeeType (:789): returns pointee ONLY for record-with-fields; NULL for scalar/union/
  empty-struct. Scalar *out uses deref state directly (whole-write).
**GAP 1 (validation, candidate)**: handleEnsureInitIfRetAttr (SemaDeclAttr:8319) + handleEnsureInitAttr
  (:8304) check isPointerType but NOT pointee writability → `const int *out ensure_init_if_ret` ACCEPTED;
  def rejected late ("'*out' not initialized at return"), caller credits uninit const. Should reject
  const-pointee upfront. /tmp/ei5.cbs, ei6.cbs.
**GAP 2 (FP, niche)**: UNION out-param — getEnsureInitPointeeType null for union → deref-store else-branch
  skips markFieldInit → field-init `out->a` NOT credited (FP: "'*out' not initialized at return"); only
  whole-write `*out=tmp` works. /tmp/uo.cbs, uo2.cbs. (union access is _Unsafe; niche.)
Both are FP/missing-validation (not soundness FN). In the area the user is actively fixing.

## ensure_init_if_ret RE-POINT handling — read+probed, SOUND & PRECISE (2026-06-08)
ReassignedParams (merge: union, :734-744) freezes deref-state promotion when the out-param is reassigned.
checkEnsureInitIfRetAtReturn uses Reassigned (:1666) to pick the EnsureInitIfRetReassigned diag.
PRECISE (not just conservative):
  - re-point BEFORE init *out → REJECTED ("'out' reassigned before '*out' initialized") — the launder
    (callee re-points to a local, inits that, caller's var stays uninit) is CAUGHT. /tmp/rep.cbs, rep2.cbs.
  - re-point AFTER initing original *out → ACCEPTED (caller's var really inited). /tmp/rep3.cbs.
  - conditional re-point with SEPARATE returns (c: init+return 0; !c: re-point+return 1) → ACCEPTED per-path
    (per-predecessor verify is path-sensitive when returns aren't joined). /tmp/rep4.cbs.
No soundness FN in the re-point path. Combined with sound delegation (cond-matched PCI), merge (meet/intersect),
field aggregation (tryPromoteParent), caller-credit (4 forms): the ensure_init_if_ret CORE is sound+precise.
The soundness holes are the LAUNDERS (F100 fnptr-cast, F104 redecl-ordering) — both known/user-fixing.

## ensure_init_if_ret DELEGATION — read+probed, SOUND (2026-06-08)
checkEnsureInitIfRetAtReturn delegation credit (:1653-1661): DS=Init only if a PCI matches
{OutParamLocal==ParamId, OutFieldIndices empty, CondValue==f's cond, RetLocal==RV.SourceLocal}.
PRECISE:
  - delegation on c-path + DIRECT return 0 on !c-path → !c-path REJECTED (PCI intersection at merge denies
    credit where inner wasn't called). /tmp/del.cbs.
  - cond-mismatch (inner cond 5, f cond 0): `return inner(out)` is non-constant → verify demands *out
    already-init on path + mismatched cond gives no credit → REJECTED. /tmp/del2.cbs.
No delegation FN. ensure_init_if_ret soundness probes ALL sound: re-point, delegation(mixed+mismatch),
merge, aggregation, caller-credit. Holes remain only F100(cast)/F104(redecl) launders; new gaps both FP.

## ensure_init_if_ret CALLER-SIDE FIELD-CREDIT — read+probed, SOUND & PRECISE (2026-06-08)
f(&_Mut x.a) credits ONLY x.a (PCI.OutFieldIndices=[a]); sibling x.b stays uninit (return x.b → "use of
uninitialized x.b"); return x.a accepted. Field-precise, no coarse whole-object credit. /tmp/fc.cbs, fc2.cbs.

## ensure_init_if_ret SOUNDNESS — COMPREHENSIVE CONCLUSION (2026-06-08, per user "not saturated")
Probed ALL constructible FN candidates — every one SOUND + PRECISE:
  re-point(before/after/conditional), delegation(mixed-path/cond-mismatch), caller field-credit(sibling),
  field aggregation(nested/partial/whole-write), merge(meet/intersect), caller-credit(4 forms/copy-prop),
  switch/bare-if(r) correctly-not-credited.
The ONLY soundness holes are the two LAUNDERS: F100 (fnptr-cast drops ExtParameterInfo contract) +
F104 (heterogeneous-redecl ordering) — both known, user fixing.
NEW robustness gaps (FP, not FN, in user's area): const-pointee validation (SemaDeclAttr:8319/8304 skip
pointee-writability) + union out-param field-init (getEnsureInitPointeeType null for union).

## meetStates / merge (:679/:688) — candidates 2026-06-17
INVARIANT: meet(A,A)=A else MaybeInit (intersection); merge treats field missing on either side as Uninitialized → meet(X,Uninit). Conservative: var/field init on only SOME paths → MaybeInit at join → read must be flagged.
Candidates:
1. [merge C5] `int x; if(c) x=1; return x;` → x MaybeInit at join → uninit read flagged; FN if meet wrong. **UNPROBED** (top)
2. [field merge] struct field set in one branch only → MaybeInit → read flagged. UNPROBED
3. [missing-entry] field present one path/missing other → meet(X,Uninit); verify symmetric (:711 vs :726). UNPROBED

## tryPromoteParent / getNumFields — EMPTY-STRUCT-FIELD FALSE POSITIVE (BSCIRInitAnalysis.cpp:929-999 + :834-842) — CONFIRMED-new 2026-06-18

**Invariant violated**: a struct all of whose *initializable* fields have been written
must be usable as a whole. A zero-field (empty) struct member has NO bytes to
initialize, so it must not block whole-struct promotion.

**THE GAP**: `tryPromoteParent` (:969-977) promotes a parent only when EVERY sibling
index `0..getNumFields(ParentTy)-1` is `Initialized`. `getNumFields` (:834-842) counts
ALL declared fields of the parent, INCLUDING an empty-struct member. The empty-struct
sibling's FieldPath is never auto-marked Initialized for a *local* (markAllFieldsInit at
entryState only runs for params/implicit-init locals; a declared local field-initialized
one field at a time never gets the empty field's path marked). `getFieldInitState` returns
`Uninitialized` for it → the sibling-loop `return`s early → parent never promotes → the
whole-struct read is wrongly flagged "use of uninitialized value: o".

**Asymmetry (one-line diff)**: replace the empty-struct field with a real int field that
is also set → promotion succeeds → ACCEPT. Position-independent (empty field first or
second). The ONLY workaround is explicitly writing `o.e = (struct Empty){};` — absurd for
a zero-byte member.

**Symptom**: FALSE POSITIVE (rejects valid + provably-safe code). Plain-C equivalent
compiles clean (-Wall) and valgrind reports 0 errors. NOT a soundness FN.

**DISTINCT from F99** (the only `tryPromoteParent` bug_log hit): F99 is OVER-promotion of a
single pointer field masking later reads (FN direction). This is UNDER-promotion caused by
a zero-field sibling (FP direction) — opposite symptom, different trigger (empty-struct
member vs deref-write-through-field).

**Blast radius**: any struct (generic or not) containing a zero-field struct/union member,
field-initialized rather than `{}`-initialized; nested empty-struct members; empty-struct as
an `ensure_init` pointee field (the contract's at-return check would also never see the
empty field promoted → likely a parallel FP at checkEnsureInitAtReturn).

**Repro**: /tmp/explorer_probe.empty_struct_field_fp.cbs.
**Baseline**: /tmp/explorer_baseline.empty_struct_field_fp.cbs (real-int sibling → ACCEPT).

---

## InitAnalysis::run zone-gating asymmetry — `transferStatement` runs UNCONDITIONALLY but the use-check is `SZ_Safe`-gated (BSCIRInitAnalysis.cpp:1981-2110) — UNPROBED (2026-06-23, _Unsafe→_Safe laundering focus)

**Invariant**: the init-state UPDATE (`transferStatement`, :2073) is zone-independent;
the init-state USE-CHECK (`checkOperand` for Assign sources :1985, terminator Call/SwitchInt/Return
:2078/2088/2092) only fires when `S.SafeZone == SZ_Safe`. So any state mutation performed inside an
`_Unsafe` block (which carries `SafeZone != SZ_Safe`) is TRUSTED by a subsequent `_Safe`-zone read.

**Architecture facts** (confirmed by source read):
- BSCOwnership.cpp has NO zone handling at all (`grep SZ_Safe` = 0 hits) → move-tracking/leak detection
  is zone-AGNOSTIC. A move in `_Unsafe` IS tracked; a use-after-move that crosses into `_Safe` is
  caught the same as one wholly in `_Safe`. (So focus (a)/(d) move-laundering is likely SOUND — verify.)
- BSCIRInitAnalysis is the ONE analyzer that zone-gates: the use-check is `SZ_Safe`-only, but
  `transferStatement` is unconditional. This is exactly F107's mechanism (the union variant write was
  inside `_Unsafe`; the laundered whole-union read was plain `_Safe`).
- `_Unsafe{}` block fully SUPPRESSES the init-use diag (note line 279). So an uninit read INSIDE
  `_Unsafe` is never flagged — only the laundering of a marked-Initialized state OUT to `_Safe` matters.

**Peers**: transferStatement (:154 the unconditional updater), checkOperand (:the gated reader),
markPointeeFullyInit (:230, fires for `*p=v` whole-pointee writes), tryPromoteParent (parent promotion),
F107 (union-variant width, :180-187), F99 (deref-store over-credit, FIXED).

**Candidates** (each must NOT be the F107 union mechanism to count as new):
1. **`*p = v` whole-pointee write in `_Unsafe` over-credits a STRUCT pointee** (:230 markPointeeFullyInit).
   If `p` points at a multi-field struct and `_Unsafe { *p = (struct S){...partial...}; }` or a single
   `*p` scalar write happens, does markPointeeFullyInit mark the WHOLE pointee Initialized so a `_Safe`
   `return *p` / whole-pointee read of an uninit tail passes? F107 is a UNION; this is a struct pointee
   via the DEREF path (different branch, :222-242, not :169-217). Reachability: `*p=v` is legal in `_Safe`
   already; doing it in `_Unsafe` changes nothing about the credit. The distinct angle = a PARTIAL pointee
   write in `_Unsafe` that still triggers markPointeeFullyInit. RANK 1.
2. **partial struct field write in `_Unsafe` then whole-struct read in `_Safe`** — per-field tracking should
   keep the unwritten sibling Uninit (markFieldInit, :215, is per-index). EXPECT SOUND (asymmetry control for
   F107: union over-promotes whole-local, struct does not). Probe as the negative/control. RANK 3.
3. **move an `_Owned` in `_Unsafe` then use in `_Safe`** — ownership is zone-agnostic → use-after-move caught.
   EXPECT SOUND. Probe to confirm the (a) hypothesis is closed. RANK 2 (cheap, closes a focus question).

**OUTCOME (2026-06-23, _Unsafe→_Safe laundering probe, 7 probes)**: surface SATURATED-SOUND
except F107. Candidate 1 (deref-pointee over-credit) = raw-ptr deref SHAPE-REJECTED in _Safe
(no reach) + borrow-deref-write launders no credit (firewall holds). Candidate 2 (partial struct)
= SOUND (per-field tracking + meetStates demotion cross _Unsafe). Candidate 3 (move) = SOUND
(BSCOwnership zone-AGNOSTIC, grep SZ_Safe=0; move tracked across boundary). The union
struct-variant partial write (u.p.x, :179-187 isUnionStructFieldPath branch) FOLDS into F107
(same whole-local LS=Initialized over-promotion as :192-204). No new root cause.

## ARRAY element-granularity vs whole-array read (transferStatement :159-217 + checkOperand :1232-1263 + run-loop :2002-2009 Rvalue::Array/Aggregate) — probing 2026-06-23 (array-analog-of-F107 hypothesis)

**HYPOTHESIS (directive)**: writing ONE array element then doing a WHOLE-array read (copy `int b[2]=a;`,
pass-by-value `sink(a)`, `return a;`) or a designated `{[0]=1}`-with-holes whole-array use launders the
uninit elements — an array-element analog of F107 (union-variant-width over-credit).

**Structural facts (source-confirmed)**:
- Element write `a[i]=v` (LOCAL array): transferStatement Assign — `getFieldPath(Dest)` (:169) returns
  None (Index in the projection chain, not pure-Field); Deref-first ensure_init branch (:224) requires
  `Projs[0]==Deref` (it's `Index`). → NEITHER branch fires → **no state change**, array stays
  `Uninitialized` in `LocalStates[a]`. (Same for array FIELD: :172 `if getFieldType(...)->isArrayType() break`.)
- Whole-array read routes through `checkOperand` with `Op.getPlace().Base == a` and `IS = getInitState(a)`
  == Uninitialized → :1319 whole-local check → REJECT "use of uninitialized value: a". PROBED-SOUND
  2026-06-04 (`int a[3]; a[0]=1; return a[2];` REJECTED). So the basic element-write-then-whole-read
  is CONSERVATIVE, NOT an F107-style over-credit. The array does NOT get the F107 treatment (F107 marks
  the WHOLE union local `Initialized` at :183/:200 on any variant write; arrays get NO such mark).
- `VisitInitListExpr` (BSCIRBuilder.cpp:1205-1228): a designated `{[0]=1}` / `{1,2}` initializer lowers
  to `Rvalue::createArray` → `emit(Assign(TmpPlace, ArrayRV))`. The run-loop (:2006-2009) iterates
  `Src.getArray().Elements` through `checkOperand` (each element operand checked), then `transferStatement`
  marks `TmpPlace` (a TEMP, whole-local) `Initialized` (:161-168). The DEST local `b` in `int b[2]={[0]=1};`
  receives `Operand::createCopy(TmpPlace)` via a SECOND Assign `b = tmp` → `b` marked Init. So a
  designated-init WITH HOLES still marks `b` fully Init (the holes are C-zero-filled at codegen, so this
  is SOUND — same as R3 aggregate-init finding, line 411-418). NOT a laundering gap.
- Whole-array COPY `int b[2] = a;` (a is a local array, NOT an init-list): lowers how? `a` as an rvalue
  is `Rvalue::Use(Operand::createCopy(Place(a, [])))` (a whole-array lvalue→rvalue conversion is a Use of
  the whole local `a`). run-loop :1989 `case Rvalue::Use: checkOperand(Src.getUse().Op=copy(a))` →
  checkOperand reads `a`'s whole-local state == Uninit → REJECT. EXPECT SOUND.
- Pass-by-value `sink(a)`: the Call terminator's `CD.Args` includes `a` as a `Use`/`Copy` operand;
  the run-loop Call case (:2078+) iterates Args through `checkOperand` → REJECT if `a` Uninit. EXPECT SOUND.

**THE OPEN CELL (the one not yet probed for the array case)**: an array-of-STRUCT where ONE element's
field is written then the whole array (or the whole element) is read. F83 already covers the
write-through-uninit-POINTER case (`s.p[i]`); this is the read-side / whole-element case. Also: does
writing `a[0].x` (a struct-element field) mark `a` Init? Per :172 the array-typed FIELD path breaks,
but `a[0].x` is `[Index, Field]` → getFieldPath None → no mark → `a` stays Uninit → whole-array read
rejected. EXPECT SOUND but UNPROBED for the struct-element shape.

**Candidates (ranked)**:
1. Whole-array read after element writes (LOCAL array) — `int a[2]; a[0]=1; sink(a);` / `int b[2]=a;` /
   `return a;` → expect REJECT (Uninit). Probes confirm SOUND. (re-confirm the 2026-06-04 result on
   the pass-by-value + copy forms, which were not individually logged.)
2. Array-of-struct partial element-field write then whole-array read — `struct S{int x,y;} a[2]; a[0].x=1;
   sink(a);` → expect REJECT (a Uninit). UNPROBED; the struct-element shape.
3. Designated-init with holes `{[0]=1}` then whole-array read — expect ACCEPT (C zero-fills holes;
   SOUND, not a launder — cf. R3 :411-418). UNPROBED for the array case.
4. Array FIELD (struct member) element-write then whole-struct read — `struct W{int a[2]; int q;} w;
   w.a[0]=1; w.q=2; return w;` → :172 break leaves `a`-field untracked; whole-struct `w` read — does
   `w` promote? tryPromoteParent needs ALL siblings Init incl. the array field `a` (never markable) →
   `w` NEVER promotes → whole-struct read REJECTED (FP, F106-cousin via the array-field sibling).
   UNPROBED. This is the array-FIELD analog of F106 (empty-struct-field) — an array field can NEVER be
   markFieldInit'd, so it blocks parent promotion identically to an empty-struct field.

### RESOLUTION of the array-element-granularity hypothesis (2026-06-23, 7 probes) — no-new-pattern
The array-analog-of-F107 hypothesis is REFUTED: arrays do NOT get the F107 whole-local-Init
over-promotion on element writes. Element writes (`a[i]=v`, local OR field) leave the array
`Uninitialized` (getFieldPath returns None for an Index-containing projection; :172 `break` for
array-typed field paths), and EVERY whole-array read is correctly REJECTED:
- `int a[2]; a[0]=1; return a[1];` → REJECT "use of uninit: a" (read uninit element).
- `int a[2]; a[0]=1; sink_arr(a);` → REJECT (whole-array pass-by-value; Call.CD.Args checked).
- `struct Pair a[2]; a[0].x=1; return a[0];` → REJECT (whole-element return of partial element).
- `struct S a[2]; a[0].x=1; return a[0].y;` → REJECT (uninit sibling field of element).
- `int a[3]={[0]=1}; return a[2];` → ACCEPT (designated holes; C zero-fills to a DEFINED value — SOUND,
  not a launder; cf R3 :411-418 aggregate-init).
The ONE false positive — `struct W{int a[2];int q;} w; w.a[0]=1; w.a[1]=2; w.q=3; return w;` → REJECT
"use of uninit: w" — is the array-field-blocks-parent-promotion case. This is NOT an F106 cousin (F106 =
EMPTY-struct field; here = ARRAY field). It is the EXACT documented `rule6_struct_error` case
(BiShengCLanguageUserManual.md §3.7.3 rule 6, line 5811 "此规则同样适用于结构体中的数组字段" + the
`rule6_struct_error` example at :5839-5845, byte-identical in shape). **FOLDED into RETRACTED F97**:
F97 (element-wise array init FP) was retracted 2026-06-08 as documented intended behavior; its retraction
note explicitly names "the struct-array-field case". Control: the `{}`-init form (documented
`rule6_struct_ok`) → CLEAN + valgrind ERROR SUMMARY 0 → PRECISION FP, no soundness hole (matches F97's
"PRECISION, not soundness" classification).
NET: the array-element init surface is SATURATED-SOUND (5 SOUND probes) + 1 documented-intended FP
(folded into retracted F97). The array all-or-nothing model is documented intended (§3.7.3 rule 6);
arrays are NOT vulnerable to the F107-style whole-local-Init over-credit because element writes never
mark the array Init. Surface closed. Probes: /tmp/explorer_probe.{L4ANrS,TLw0tr,x5WB1h,uEQOWL,sM7wBY,
AYaM6v}.cbs + /tmp/explorer_baseline.{eaKeEW,3oRPWi}.cbs.


## array-typed FIELD member blocks whole-struct promotion (const-generic Array<T,N>) — probing 2026-06-23 (directive: const-generics × init-analysis)

**Hypothesis (directive)**: a const-generic struct `struct Array<T,int N>{ T data[N]; };` instantiated `Array<int,3> a;` then field-initialized (`a.data[0]=1`) and used WHOLE (`sink(a)` / copy / return) in `_Safe` — is the uninit array tail (data[1],data[2]) caught? The directive frames this as the array-member analog of F106 (empty-struct-blocks-promotion FP) / F107 (union-variant-width FN) / F83 (indexed-write-dest), and asks specifically: does an EMPTY array `Array<int,0>` block promotion like F106's empty-struct FP, or LAUNDER a whole-struct use (FN)?

**Invariant that should hold**: a struct all of whose *initializable* fields have been written must be usable as a whole (return / pass-by-value / copy) — cf. F106. Conversely, a struct with a genuinely-uninit field must REJECT the whole-struct read (soundness).

**Source-confirmed mechanism** (BSCIRInitAnalysis.cpp):
- `getNumFields` (:834-842) counts EVERY declared field via `field_begin()..field_end()` — NO skip for array-typed fields. So an array member IS a counted sibling.
- `transferStatement` Assign field-path branch (:155-156): `if (getFieldType(FP->Base, FP->Indices)->isArrayType()) break;` — a write to an array-typed field (or element thereof, since the Index projection makes `getFieldPath` return None anyway) NEVER calls `markFieldInit`. This is the documented array-element limitation ("arrays must be init'd via {} or __assume_initialized").
- `tryPromoteParent` (:929, sibling loop :970-977) promotes the parent ONLY when EVERY sibling index in `[0,getNumFields)` is `Initialized`. The array-typed sibling can NEVER reach `Initialized` via a field/element write → the all-siblings test (:975) never holds → parent NEVER promotes → a whole-struct read (`sink(a)`, `return a`, `struct Array b = a`) routes through `checkOperand` whole-local check → REJECTED "use of uninitialized value".
- This is the FP direction (rejects valid+provably-safe code), the same DIRECTION as F106 — NOT an F107-style over-credit (no path marks the whole struct Init from an element write; the `:155 break` ensures no state change).

**Why this may be DISTINCT from F106 (not an automatic fold)**:
- F106's unmarkable sibling = an EMPTY STRUCT (zero sub-fields; nothing to write). Its proposed fix = "skip zero-field siblings in getNumFields".
- Here the unmarkable sibling = an ARRAY (has elements, but `transferStatement:155` explicitly `break`s on array-typed fields). The array is NOT a zero-field struct — F106's "skip zero-field siblings" fix would NOT cover it. Different sub-mechanism (`:155 isArrayType break` vs "no sub-field to write"), different fix surface (skip array-typed siblings in the promotion count, OR permit whole-array-field marking on `{}`-init of the element type).
- The const-generic `Array<int,N>` wrapping makes the array field a FIRST-CLASS pattern (the documented generics-guide `Array<T,int N>{T data[N];}` example), so the FP bites a canonical idiom, not a contrived empty-struct.
- The `Array<int,0>` empty-array case is a genuine boundary: N=0 yields `data[0]` (zero-length array). If it compiles, does it block promotion (F106-fold FP) or launder (FN)? UNPROBED.

**Peers**: F106 (empty-struct sibling, FP, same tryPromoteParent/getNumFields root — the fold candidate), F107 (union-variant over-credit FN, OPPOSITE direction), F83 (write-through-uninit pointer field, FN), the documented array-element limitation (:155 break + :236 array-field-pointee break).

**Candidates (ranked)** — PROBED 2026-06-23, ALL FOLDED into F106 (no new filing):
1. **`Array<int,3> a; a.data[0]=1; return a;` → FP REJECT** (PROBED-FOLD-F106) (the array sibling blocks promotion; whole-struct use of an otherwise-all-init struct rejected). The canonical-idiom FP. PROBE FIRST.
2. **`Array<int,3> a; a.data[0]=1;a.data[1]=2;a.data[2]=3; sink(a);` → STILL FP REJECT?** (all elements written but the array field is STILL never markFieldInit'd → still blocks) — sharpens the FP (no element-wise workaround, unlike scalar fields). PROBE.
3. **`Array<int,0> a; ... sink(a);`** — does the zero-length array even compile? if so, does it block promotion (F106-fold) or launder (FN)? PROBE (the directive's empty-array question).
4. **Non-generic `struct W{int a[2]; int q;} w; w.a[0]=1;w.a[1]=2;w.q=3; return w;`** — the array-FIELD (non-generic) form; confirms it is NOT generic-specific (generic is only blast radius). PROBE (baseline-shape).

**Expected verdict**: FP in the F106 family (under-promotion via an unmarkable array sibling).

**RESOLUTION (2026-06-23, 8 probes)**: FOLDED into F106 (no new filing). All four FP shapes reproduce (const-generic `Array<int,3>` 1-elem + all-elems + `Array<int,0>` empty + non-generic `struct W{int a[2];int q;}`); asymmetry proven (scalar-field control ACCEPT, `{}`-workaround ACCEPT, genuinely-uninit control C REJECT). Root identical to F106: `getNumFields` (:834-842) counts the unmarkable array-typed sibling; `transferStatement:155 isArrayType break` ensures it can NEVER be `markFieldInit`'d; `tryPromoteParent` (:970-977) all-siblings-Init fails → parent never promotes. Discriminator E confirms F106 is STILL OPEN on 34e6f26e (identical symptom) → this is a sibling variant of the same open root, NOT a re-open. The directive's F107-style laundering (FN) hypothesis REFUTED by control C (under-promotion, not over-credit); F83 hypothesis REFUTED (opposite direction). The const-generic `Array<T,int N>{T data[N]}` wrapping is a canonical-idiom blast-radius variant (strengthens F106's severity); the fix surface differs in detail (array-sibling-skip vs zero-field-sibling-skip) but is the SAME family/root — F106's blast-radius note already covers generic structs + unmarkable siblings. Recommend Conductor append the array-field-sibling case to F106's blast-radius note. The fold-vs-distinct decision rides on whether the fix surface differs (array-skip vs zero-field-skip) — likely DISTINCT-but-same-family, Conductor decides filing. If `Array<int,0>` LAUNDERS (FN), that is a genuinely-new root (over-credit, F107-style) and unconditionally CONFIRMED-new.

## __assume_initialized handler (BSCIRInitAnalysis.cpp:432-470) — read 2026-06-24
**Invariant**: `__assume_initialized(arg)` marks the addressed place init WITHOUT contract verification
(user-asserted). Shapes: `&x`→whole local + markAllFieldsInit + (if ensure_init param) markPointeeFullyInit;
`&*p`→pointee; `&p->f`→field. Repointed param gates pointee promotion (:445,452).
**Peers**: markPointeeFullyInit (:1017), markAllFieldsInit (:1033), checkEnsureInitAtReturn, F58 (side-effect
arg drop), the Sema "arg cannot contain array subscript" gate (6-init:425).
**Candidates**: 1. **`__assume_initialized(&out)` in `_Safe` satisfies ensure_init w/o writing *out** — if
accepted in _Safe (not gated to _Unsafe) it's a contract-launder escape reachable from the safe zone. UNPROBED ⭐.
2. `&out` marks BOTH out + *out (over-broad vs `&*out`). 3. markAllFieldsInit unconditional recursion on `&x` struct.

## short-circuit init merge (&&/|| conditional init) — probe 2026-06-24
**Invariant**: a write `x=5` in the RHS of `c && (x=5)` runs only on the c-true path; after the `&&`,
x must be MaybeInit (init on one CFG path, uninit on the other), so a later read is flagged.
**Peers**: transferStatement, OwnershipImpl::merge (the ownership twin), CFG short-circuit lowering.
**Candidates**: 1. **`c && (x=5); return x;` — x merged as fully Init (FN) vs MaybeInit (sound)** UNPROBED ⭐.
2. `c || (x=5)` (init on c-FALSE path). 3. nested `(a && (x=1)) || (b && (x=2))`.

## array element write→read init tracking (transferStatement :218-240) — probe 2026-06-24
**Invariant**: `arr[i]=v` does NOT mark the whole array Init (need {}/__assume_initialized). Question:
does it mark the ELEMENT, so reading the just-written `arr[i]` is clean — or is element granularity absent
→ reading a written element is wrongly flagged uninit (FP)?
**Peers**: markFieldInit (array guard :236), getFieldType isArrayType (:172), __assume_initialized.
**Candidates**: 1. **`arr[0]=5; return arr[0];` — flagged uninit (FP, over-conservative) vs clean** UNPROBED ⭐.
2. write ALL elements then read (does N writes promote the array?). 3. read a DIFFERENT (unwritten) element.

## ensure_init_if_ret conditional-credit branch polarity (terminator :496-545) — probe 2026-06-24
**Invariant**: a call to `ensure_init_if_ret(N)` records a PendingCondInit credited ONLY on the
return-edge where retval==N (comment :502-505: unconditional marking is unsound). Reading *out on a
NON-matching return branch must stay uninit.
**Peers**: classifyEnsureInit, ComparisonFacts, SwitchInt-edge credit, F104 (redecl launder leg).
**Candidates**: 1. **read x on the `r != 0` branch after `r = inner(&_Mut x)` [eiir(0)] — flagged uninit
(sound) vs credited (FN)** UNPROBED ⭐. 2. credit through a copied return var. 3. eiir(N) with N≠0.

## ensure_init satisfied only inside a maybe-zero-trip loop — probe 2026-06-24
**Invariant**: ensure_init requires *out init on ALL return paths; a write `*out=i` only inside `for(i<n)`
leaves *out uninit on the n==0 (loop-skipped) path → contract must FAIL.
**Peers**: checkEnsureInitAtReturn, loop CFG merge, markPointeeFullyInit.
**Candidates**: 1. **`for(i<n) *out=i;` ensure_init — rejected on n=0 path (sound) vs credited unconditionally
(FN, caller reads uninit *out)** UNPROBED ⭐. 2. while(c) variant. 3. write in both loop + after (always inits).

## ensure_init contract delegation (call another ensure_init fn with own out-param) — probe 2026-06-24
**Invariant**: `outer(out [ensure_init])` calling `inner(out)` where inner also has ensure_init must credit
*out init via the delegated call (terminator handler delegation branch :521-531) → outer's contract satisfied.
**Peers**: classifyEnsureInit, asCopiedLocal, markPointeeFullyInit.
**Candidates**: 1. **delegation `inner(out)` credits *out (outer accepted, sound) vs not (FP)** UNPROBED ⭐.
2. delegate a re-pointed out (must NOT credit). 3. delegate only on one branch.

## F88 sibling: uninit _Safe fnptr FIELD callee `s.fp()` (MemberExpr callee) — probe 2026-06-24
**Invariant**: the F88 fix use-checks a call callee for init; must cover MemberExpr (field) callees, not
only DeclRefExpr (variable) callees, else an uninit `s.fp()` slips (FN sibling of F88).
**Peers**: F88 (DRE callee, fixed), F31 (_Nullable fnptr callee), VisitCallExpr callee-position.
**Candidates**: 1. **uninit field fnptr `s.fp()` — flagged (sound, fix covers MemberExpr) vs not (FN sibling)** UNPROBED ⭐.
2. `(*pfp)()` deref-callee uninit. 3. arr[i]() indexed callee.

## InitAnalysis::merge + meetStates (BSCIRInitAnalysis.cpp:680-789) — read 2026-06-25
INVARIANT: a proper MEET (greatest-lower-bound) join. meetStates(:680): A==B→A, else→MaybeInit (Init+Uninit /
Init+MaybeInit / MaybeInit+Uninit all → MaybeInit). merge(:688): LocalStates meet pairwise; FieldStates — a field
entry MISSING on EITHER side is treated as `Uninitialized` before the meet (:711 src-missing→dst-uninit, :726
dst-missing→src-uninit), so init-on-one-branch + missing(=uninit)-on-other → MaybeInit (correctly flagged at use).
ReassignedParams: UNION (a re-point on any path freezes promotion — conservative for ensure_init; collects both
sides' sites for the at-return note).
KEY CONTRAST WITH F75 (why init-merge is SOUND but ownership-merge is BUGGY): the init-merge's safe default for a
missing field-entry is UNINITIALIZED (→ MaybeInit on meet → flagged); F75's OwnershipImpl::merge set-UNIONs the
owned-field set, so a field moved-on-one-branch (absent from the other branch's moved-set) is RESTORED to owned at
the join → double-free FN. Same C5 family, opposite (correct vs incorrect) default direction. Probe-confirmed
2026-06-25 (conditional/switch/do-while field+local init all flag MaybeInit correctly).
CANDIDATES (all resolved/sound): (1) missing=Uninitialized default — CORRECT safe default; (2) ReassignedParams
union — intentional conservative freeze; (3) meetStates symmetric+total — correct. merge SOUND.

## tryPromoteParent (BSCIRInitAnalysis.cpp:929-1000) — read 2026-06-25 (both known bugs localized; no new candidate)
INVARIANT: after a field is markFieldInit'd, promote the parent to Initialized iff ALL its siblings are Initialized
(then recurse upward); a 0-field UNION parent is promoted from ANY single variant write (one variant "covers all
bytes").
KNOWN BUGS visible here:
- F106 (empty-struct sibling): getNumFields counts an empty-struct member as 1 field with 0 sub-fields; the
  all-siblings-Initialized test (:970-977) can never see it markFieldInit'd → parent never promoted → FP.
- F107 (union over-promotion): the NumSiblings==0 union arm (:948-964) marks the WHOLE union Initialized from any
  single variant write WITHOUT verifying the written variant covers all the union's bytes; a NARROW variant write
  then lets a wider/other-variant READ pass → FN.
CANDIDATES (no NEW): (1) NumSiblings==0 non-union (empty-struct PARENT) returns unpromoted (:966) — unreachable
(a field FP cannot have a 0-field parent); (2) upward recursion (:998/:961) — standard, sound; (3) union recursion
after promote (:961) — correct (promotes enclosing struct if its siblings init). tryPromoteParent's defects are
exactly F106+F107; rest sound.

## markPointeeFullyInit / markAllFieldsInit (BSCIRInitAnalysis.cpp:1017-1059) — read 2026-06-25
INVARIANT: when an ensure_init pointee `*out` is FULLY initialized, mark its EnsureInitDeref state + EVERY field
(recursively, incl. nested struct/union variants) Initialized.
CANDIDATE (FOLD-F107, not new): markAllFieldsInit (:1041-1058) marks ALL fields init including ALL UNION variants
(:1046-1048 recurses into union variants, :1055 marks each) — for a union pointee fully-written via a single
variant, this marks every variant init → a later read of a DIFFERENT (inactive) variant passes (type-pun FN).
Same union-over-promotion root as F107 (tryPromoteParent union arm), reached via the ensure_init full-write path
instead of the direct-variant-write path. Per discipline (don't probe confirmed-defect variants) NOT separately
probed/filed; recorded as F107 blast-radius. Rest sound (full-write → all-fields-init is correct for non-union).

## getFieldPath / getFieldPathPrefix / markFieldInit (BSCIRInitAnalysis.cpp:864-927) — read 2026-06-25
INVARIANT: getFieldPathPrefix walks a Place's projections building a FieldPath, STOPPING at the first non-Field
projection (Deref/Index break the loop, :894-896); getFieldPath returns the path ONLY if EVERY projection is a
Field (Indices.size()==Projections.size(), :866) — else None. markFieldInit(Place) (:923) no-ops when getFieldPath
is None. Walks THROUGH union variants (union field access = Field proj, :891-893 uses Proj.ResultTy).
KNOWN ROOTS visible here (no new candidate):
- F99 (deref-store `*b.p=v` not tracked): the Place has a Deref projection → getFieldPath returns None →
  markFieldInit(Place) no-ops → the pointee field never marked init. (getFieldPathPrefix stops at the Deref.)
- F97 (array element `s.arr[i]=v` / `arr[i]=v` not tracked): Index projection → getFieldPath None → array stays
  whole-granularity uninit (= F97 retracted-but-intended).
CANDIDATES (no new): (1) Deref→None = F99; (2) Index→None = F97; (3) union-variant walk via Proj.ResultTy
(:893) — relies on the IR builder's precomputed ResultTy, correct for the probed shapes. getFieldPath underlies
F99+F97; rest sound.

## transferTerminator (BSCIRInitAnalysis.cpp:421-680) — read 2026-06-25 (Call terminator init effects)
INVARIANT: at a Call terminator — (1) the call Dest local is marked Initialized (:428-430); (2) __assume_initialized
marks the addressed memory init across arg shapes &x / &*p / &p->f (:438-470) with RE-POINTED-PARAM GATING (:445,
452,456 — a reassigned param's &*p denotes the new pointee, not the contract-tracked one, so skip — F45-class
stale-state handling done correctly); (3) caller-side ensure_init params marked *param-init via CD.Decl OR
CalleeProtoType (:478, the indirect-call fallback = F88-area, handled).
CANDIDATES (no new): (1) __assume_initialized laundering uninit→init — BY DESIGN (explicit escape, programmer
asserts init); (2) markAllFieldsInit(&x) marks all union variants init (:450) = F107 blast radius (already noted);
(3) re-pointed-param gating — careful + correct (mirrors deref-write gating). transferTerminator SOUND/careful;
the F45/F88/F104 areas are handled here. init-analysis now documented end-to-end (entry/transferStmt/transferTerm/
merge/promote/field-path/mark*).

## entryState (BSCIRInitAnalysis.cpp:111-152) — read 2026-06-25 (init-analysis entry lattice)
INVARIANT: function-entry init states — return slot _0 = Uninitialized; params (1..NumParams) = Initialized +
markAllFieldsInit (caller-initialized); other locals = Uninitialized EXCEPT globals/statics/va_list
(isImplicitlyInitialized → Initialized, correct: C zero-inits statics/globals); ensure_init & ensure_init_if_ret
params → EnsureInitDerefStates[*param] = Uninitialized (callee must init, checked at return).
CANDIDATES (no new): (1) markAllFieldsInit(:123) marks union-variant params all-init = F107 blast radius (param IS
caller-init so benign); (2) isImplicitlyInitialized globals/statics/va_list → correct (zero-init/opaque);
(3) ensure_init_if_ret deref uninit (:146) → correct (conditional contract). entryState SOUND.
init-analysis DOCUMENTED END-TO-END: entryState + transferStatement(F99) + transferTerminator(F88/F45/F104) +
merge(MEET-sound, cf F75) + tryPromoteParent(F106/F107) + markPointeeFullyInit/markAllFieldsInit + getFieldPath
(F99/F97). Defects = F88/F90/F98/F99/F104/F106/F107/F97; rest sound. init-analysis frontier COMPLETE.

## transferStatement Assign case (BSCIRInitAnalysis.cpp:159-220) — read 2026-06-25 (per-statement def, F97/F107 roots)
INVARIANT: an Assign marks its Dest init — local → LocalStates[Dest]=Init; field-path → markFieldInit (with
union special-casing); array-typed field → BREAK (not tracked); array element (arr[i]) → not reached (getFieldPath
None). Union handling: writing a struct-field-within-a-union-variant (u.s.x) markFieldInit + marks whole-local
Init (top-level) so cross-variant reads (u.f) pass; writing a whole variant clears field tracking + marks union Init.
KNOWN ROOTS visible (no new candidate): F97 (array-field break :172-173 + array-elem not-reached :218-220 — arrays
all-or-nothing); F107 (union over-promotion :180-188 — narrow u.s.x write marks whole union Init → cross-variant
read passes). F99 (deref-store *param) is in the later *param-write portion (:222+). Normal struct-field init
tracked correctly. transferStatement Assign defects = F97+F107; rest sound.

## checkOperand (BSCIRInitAnalysis.cpp:1223-1408) — read 2026-06-25 (use-check, def+use docs now complete)
INVARIANT: at each operand USE — Constant → skip; whole-local Initialized → OK except a struct-field-within-union-
variant read WITH active field entries (partial union-struct write) → report field state (Uninit→UseOfUninit /
MaybeInit→UseOfMaybeUninit); else field-level state checked (field-init → OK; nested-union cross-variant read with
field entries under the union prefix → OK). Emits InitDiagKind::UseOfUninit / UseOfMaybeUninit.
CANDIDATES (no new): (1) whole-local-Init → cross-variant union READ passes (:1238-1262) = F107 over-read (the
read side of the F107 over-promotion; narrow variant write marked whole-local-init → wider read passes); (2)
temps/unnamed locals skip the diag (:1249-1250) — noise-avoidance, temps init by defining expr, low concern.
checkOperand careful+comprehensive. INIT-ANALYSIS DEF+USE FULLY DOCUMENTED (transferStatement def-side +
checkOperand use-side + transferTerminator + merge + promote + field-path + entry + ensure_init checks).

## checkEnsureInitAtReturn / analyzeReturnValue / checkEnsureInitIfRetAtReturn (BSCIRInitAnalysis.cpp:1511-1660) — read 2026-06-25
INVARIANT: at a return, for each ensure_init deref-tracked param, if *param is Uninit/MaybeInit → emit
EnsureInitNotInit / EnsureInitMaybeNotInit / EnsureInitReassigned (re-point attributed via NoteLocs). if_ret
params skipped here, handled by checkEnsureInitIfRetAtReturn using analyzeReturnValue (traces the return value:
folds constants, follows _0=copy(_t) one level, for the conditional "init-if-ret-equals-N" contract).
CANDIDATES (no new): the check correctly ENFORCES the contract when the EnsureInitDerefState entry exists. F104
(heterogeneous-redecl ensure_init laundering) is UPSTREAM — the contract entry is dropped at redecl so this check
has nothing to enforce; F100 (cast laundering, now fixed) similarly upstream. checkEnsureInit* SOUND (enforcement
correct; defects are in contract-attachment/redecl paths). INIT-ANALYSIS NOW FULLY DOCUMENTED end-to-end incl.
ensure_init contract checks.

## checkEnsureInitAssign / checkEnsureInitDerefReads (BSCIRInitAnalysis.cpp:1409-1510) — read 2026-06-25 (EVERY init-fn now read)
INVARIANT: prevent aliasing/reading an ensure_init pointer before its *param is initialized. checkEnsureInitAssign:
copying the param ptr into a TEMP → tracked (TempToEnsureInitParam); into a NAMED var before init → REJECTED
(EnsureInitPtrAliased — can't track the alias across blocks). checkEnsureInitDerefReads: reading *p / p->f before
*p init → diag. Sound (temp=block-local tracked, named=cross-block rejected; conservative+correct).
CANDIDATES (no new): the temp-vs-named gating is sound (the only unsound case — cross-block alias — is rejected).
ALL init-analysis functions now read: entry/transferStatement/checkOperand/transferTerminator/merge/tryPromoteParent
/markPointeeFullyInit/markAllFieldsInit/getFieldPath/markFieldInit/getFieldType/getNumFields/checkEnsureInit{Assign,
DerefReads,AtReturn,IfRetAtReturn}/analyzeReturnValue. Defects = F88/F90/F97/F98/F99/F104/F106/F107; ALL ELSE SOUND.
BSCIRInitAnalysis.cpp FULLY READ + DOCUMENTED.

## checkOperand (BSCIRInitAnalysis.cpp:1223) — use-of-uninit gate (2026-06-27 Mode-1)
INVARIANT: an operand use is OK iff its place is Initialized. Constant→skip. Whole-local Initialized → field accesses fine
EXCEPT a struct field inside a UNION VARIANT with active field-level tracking (isUnionStructFieldPath + hasUnionFieldEntries):
then it checks the FIELD init state and emits UseOfUninit if the field is Uninitialized (temps/unnamed suppressed). This is
exactly the F107 surface (open MEDIUM FN: a narrow union-variant write over-promotes the whole union to Initialized, so a
sibling-variant field read is wrongly accepted). PEERS: transferStatement (:154), transferTerminator (:421), checkEnsureInit*
(:1409-1608 = ensure_init attr, F104/F113 area). The union field-level tracking is intricate; F107 is the known gap. Not
re-probed (filed). Other init paths (plain locals, maybe-init, fields) PROBED-SOUND across the session.

## transferStatement (BSCIRInitAnalysis.cpp:154) — init-state transfer dispatch (2026-06-27 Mode-1)
Assign: Dest local → LocalStates[Dest]=Initialized. Dest field-path → array fields can't be element-init (need {} /
__assume_initialized); UNION-struct field write → markFieldInit + (UnionDepth==0) mark whole local Initialized "so
cross-variant reads pass". THIS IS F107's ROOT IN CONTEXT: a narrow union-variant field write over-promotes the whole
union to Initialized, so a sibling-variant field read is wrongly accepted (open MEDIUM FN, filed). transferStatement +
checkOperand together = the init-analysis core; both read, F107 the sole known gap. Init-analysis core mapped.

## checkEnsureInitAtReturn (BSCIRInitAnalysis.cpp:1511) — ensure_init out-param enforcement (2026-06-27)
INVARIANT: at each return, every `__attribute__((ensure_init))` param whose deref-state is Uninit/MaybeInit → diag
(EnsureInitNotInit / EnsureInitMaybeNotInit / EnsureInitReassigned). ensure_init_if params skipped here (getIfRetCondValue)
→ deferred to checkEnsureInitIfRetAtReturn (per-path). PEERS: checkEnsureInitAssign/DerefReads/IfRetAtReturn (1409-1608).
CANDIDATES: 1. (ensure_init_if_ret heterogeneous-redecl launder = F104, open, DO-NOT-FILE user area). 2. basic single-zone
ensure_init uninit-at-return → should be caught (positive control). UNPROBED → probing. 3. MaybeInit→diag conservative, sound.

## transferTerminator (BSCIRInitAnalysis.cpp:421) — terminator init effects (2026-06-27)
INVARIANT: Call terminator → Dest local Initialized (call return-inits it); __assume_initialized(arg) marks addressed memory
init by SHAPE: &x=whole local+all fields+pointee(if ensure_init param), &x.f=specific field, &*p=pointee, &p->f=field of
pointee — gating RE-POINTED params (a re-pointed &*p denotes the new pointee, don't promote). PEERS: transferStatement(:154),
checkOperand(:1223). F58 (fixed) was __assume_initialized side-effect-arg. CANDIDATES: 1. __assume_initialized(&s.a)
over-marking sibling s.b init → use-of-uninit FN? 2. goto/branch skipping an init → use-of-uninit caught (CFG)? UNPROBED→probing 2.
3. re-point gating sound.

## init flow through IRREDUCIBLE CFG (2026-06-27, PROGRESS.md ⬜ row)
INVARIANT: init-analysis is CFG fixed-point (transferStatement/transferTerminator over the CFG); must handle irreducible CFGs
(goto jumping INTO a loop body = multiple entry points) — a use reachable via a path that skips the init must be flagged
maybe-uninit. CANDIDATES: 1. goto-into-loop-body skipping p=mk() then consume(p) on the goto path → use-of-(possibly)-uninit
caught? UNPROBED→probing. 2. irreducible back-edge init-state convergence. 3. CFG fixed-point handles multi-entry.

## InitAnalysis::merge / meetStates (BSCIRInitAnalysis.cpp:679-726) — CFG-join meet — PROBED-SOUND 2026-06-29
**Invariant**: at a CFG join, a local/field's init state is the MEET of predecessors. `meetStates` (:680) is a proper
3-element lattice meet: `A==B → A`, else → **MaybeInit** (Init+Uninit, Init+MaybeInit, MaybeInit+Uninit all → MaybeInit).
Missing FieldStates entries on either side are treated as Uninitialized (:706,711,726: `meetStates(x, Uninitialized)`) —
sound (uninit-if-uninit-on-any-path). A MaybeInit value fails the use-check.
**Peers**: meetStates, intersectMapByValue (:94), transferStatement, tryPromoteParent.
**Probe** `int x; if(c){x=5;} return x;` → REJECT "use of possibly uninitialized value: `x`"; field `s.b` init on one
branch → REJECT "use of possibly uninitialized value: `s.b`"; init-on-both-branches → ACCEPT.
**CROSS-ANALYZER C5 CONTRAST** (2026-06-29): ownership's merge (F75, BSCOwnership.cpp:254-276) UNIONs the owned-field
set with NO maybe-moved element → unsound double-free FN; init's merge HERE has the MaybeInit element and meets
correctly → sound. nullability's mergeVD (BSCNullabilityCheck.cpp:849) also meets correctly. **F75 is the lone
merge-state defect**; its fix = add a maybe-moved lattice element mirroring init's MaybeInit.
**Candidates**: 1. (C5 merge under-approx) REJECTED — proper meet w/ MaybeInit, sound. 2/3 covered (note thorough).

## checkEnsureInitDerefReads (BSCIRInitAnalysis.cpp:1455) — ensure_init deref-read check — read 2026-06-29
**Invariant**: reading `*p` where p is an `ensure_init` param, before the callee has initialized the pointee, is
rejected (EnsureInitDerefReadUninit). Gated on `P.Projections[0].K == Deref` (only fires when Deref is the FIRST
projection — a direct `*p`). **Peer**: transferStatement deref-store (F55, SAME Projections[0]==Deref gate on the
WRITE side, BSCIRInitAnalysis.cpp:224). **Candidates**:
1. (Projections[0]!=Deref read-skip, F55-read-analog, PROBE) a FIELD-deref or deeper read of an ensure_init param's
   pointee where Deref is NOT the first projection → check skipped → uninit pointee read not caught (FN).
2. temp-alias resolution (line ~17-22) — alias to original param; a mis-resolved alias skips the check.
3. EnsureInitDerefStates absence (line 23-25) → return (untracked → no check).

## tryPromoteParent (BSCIRInitAnalysis.cpp:929) — parent-init promotion — read 2026-06-29
**Invariant**: when a field is init, if all siblings are now init, promote the parent to Initialized (recurse up).
For a UNION parent, promoting ANY single variant marks the whole union Initialized (line 948-958) = **F107 site**
(union variant-width FN, filed). Sibling count via `getNumFields(ParentTy)`. **Peers**: markFieldInit, meetStates, getFieldType, getNumFields. **Candidates**:
1. (F107 union variant-width — FILED).
2. (getNumFields miscount edge, PROBE) struct with ANONYMOUS member / bitfield → if sibling count is wrong, parent promoted before all real siblings init → uninit-sibling read FN.
3. nested promotion depth (recurse-up correctness).

## tryPromoteParent (BSCIRInitAnalysis.cpp:913-1000) — init-state parent promotion
- **Invariant**: marking a field Initialized promotes the parent to Initialized iff ALL siblings are Initialized (struct fully init ⇒ usable by value); recurses upward. Union: any one variant promotes the whole (F107: unsound for narrow variants).
- **Peers**: markFieldInit (calls it), getNumFields/getFieldType (sibling count), clearUnionFieldEntries, FindNonnull (init×nullability).
- **Candidates**: (1) **PROBED-F107**: union NumSiblings==0 branch promotes whole union on any single variant write — narrow-variant FN (filed). (2) **PROBED-SOUND 2026-06-30**: anonymous struct member with a `_Nonnull` field — init `s.x`+`s.p` (anon) → `use(s)` clean (promoted correctly); init only `s.x` (anon `s.p` uninit) → "use of uninitialized value: s" (promotion correctly blocked). getNumFields counts anon members right; no promotion-too-early FN. (3) **PROBED-SOUND 2026-06-30 (F97-consistent)**: struct with an array field — element-wise `s.arr[0]=..;s.arr[1]=..` does NOT promote the struct (`use(s)` → "use of uninitialized value: s", over-strict but SAFE, matches F97 whole-array granularity); whole-struct `{1,{2,3}}` init → clean. Array field treated as one opaque field that is init only via whole-array init. No FN.
