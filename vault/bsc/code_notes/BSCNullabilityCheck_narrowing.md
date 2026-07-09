# BSCNullabilityCheck.cpp — narrowing-CFG notes (E6 surface)

Companion note to `BSCNullabilityCheck.md` (which is CONTESTED with E1). This file
holds the PURE-nullability CFG-narrowing-propagation analysis (no ownership/move cross).

Source: `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp`.

## E6 narrowing-CFG section — 2026-05-30 (Explorer)

### State-track invalidation asymmetry — `VisitBinaryOperator` base reassignment (:613-648) — CONFIRMED-new

**Invariant**: when a base-pointer VarDecl `s` is reassigned (`s = other()`), EVERY
path-sensitive narrowing fact keyed on `s` must be invalidated — including the per-VD
nullability (`CurrStatusVD`), the per-DerefPath nullability (`CurrStatusDPVD`, e.g. `*s`),
AND the per-FieldPath nullability (`CurrStatusFP`, e.g. `s->f`). After the reassignment, `s`
points at a different object, so prior refinements of `s`, `*s`, and `s->f` no longer hold.

**Three parallel state maps** (all keyed, directly or via base, on the VarDecl):
- `CurrStatusVD`: VarDecl* → NullabilityKind.
- `CurrStatusDPVD`: DerefPathVD(VarDecl,depth) → NullabilityKind  — `*s`, `**s`.
- `CurrStatusFP`: FieldPath(VarDecl,suffix) → NullabilityKind     — `s->f`, `s.f`.

**The bug**: `VisitBinaryOperator`'s VarDecl-LHS branch (:613-631) handles VD reassignment:
- VD-track: line 622-624 updates `CurrStatusVD[VD] = RHSKind` (correct re-widen).
- DPVD-track: line 631 calls `InvalidateDerefStatusForVar(VD)` → clears all `(VD,*)` deref facts.
  The comment at :626-630 explicitly motivates this: "Assignment-time rebinding can stale
  existing dereference-chain facts ... The old (*p) refinement no longer applies after p is
  reassigned."
- **FP-track: NOTHING.** There is no `InvalidateFieldStatusForVar(VD)`. `CurrStatusFP`
  entries whose FieldPath base is `VD` are left STALE. (grep confirms: no FP-invalidation
  function exists anywhere in the file; the only `CurrStatusFP` writes are the narrowing-set
  at :512/:645 and the read at :416/:763.)

**Consequence (soundness FN)**: a `_Nullable` field narrowed via `if (s->f)` stays NonNull in
`CurrStatusFP[{s,"->f"}]` after `s = other()`. The post-reassignment `*s->f` consults the stale
NonNull (getExprPathNullability MemberExpr arm, :408-419 → `CurrStatusFP[FP]`) and is ACCEPTED.
When `other()` returns a struct whose `.f` is null → runtime Invalid read at 0x0 / SIGSEGV.

**Root-cause-pinning ASYMMETRY**: the scalar/DerefPath TWIN is correctly handled —
`if (*pp){**pp; pp = other2(); **pp;}` is REJECTED at the second `**pp` because the DPVD
invalidation at :631 DOES fire. Identical CFG shape, opposite verdict → isolates the missing
FP-invalidation as the sole cause (not a merge/population/laundering issue).

**Distinct from**:
- F26 (`mergeDPVD` asymmetric meet at a CFG JOIN; DPVD-only; about absent-key default, NOT
  about assignment-time invalidation). Mine is a transfer-function event, not a join.
- F33 (`(*X).f` FieldPath nullptr-collision at the POPULATION point via VisitMEForFieldPath
  skipping UO_Deref). F33's own writeup says the ARROW form `s->f` does NOT collide — and my
  repro uses the arrow form, so the FP key is well-formed `{s,"->f"}`; the bug is staleness,
  not mis-keying.
- F45 (stale `SNullOwnedFields` after a reassign chain) — OWNERSHIP analyzer (BSCOwnership.cpp,
  `checkSFieldAssign`), different file/state-container.
- F18/F48/F50 (Nullable laundered via pointer arith / compound-assign value/state).
- F70 (`NullCheckInfo::invert` broken De-Morgan over a compound condition).

**Defect class**: C7 (narrowing not correctly "un-narrowed"/propagated) with a C1
sibling-asymmetry flavor (DPVD-track has the invalidation peer that the FP-track lacks).

**Severity**: HIGH — pure nullability soundness FN under `-nullability-check=all`; runtime null
deref; idiomatic "narrow a field, then advance/replace the cursor struct" pattern (linked-list /
iterator walk: `if (node->val) { use(*node->val); node = node->next; use(*node->val); }`).

**Fix surface**: add an `InvalidateFieldStatusForVar(VD)` (erase every `CurrStatusFP` entry whose
FieldPath base VarDecl == VD) and call it right after :631, symmetric to the DPVD invalidation.
A unified `InvalidateAllStatusForVar(VD)` covering DPVD+FP is the clean form.

**Repro**: `/tmp/E6_repro_field_narrow_stale.xmbO7L.cbs` (clean compile + vg Invalid-read-0x0).
**Asymmetry baseline**: `/tmp/E6_baseline_scalar_dpvd.KHMqHN.cbs` (scalar DPVD twin, correctly rejected).
**Probe ledger**: `/tmp/probed_E6.md` (8 CFG shapes; only shape 8 = new gap).

## R2 invalidation-events — 2026-05-30 (Explorer, Chain X continuation, F84 follow-up)

### Field-write does NOT invalidate DEEPER FieldPath narrowing — `VisitBinaryOperator` MemberExpr-LHS branch (:632-647) — CONFIRMED-new

**Invariant**: a write to an intermediate field `s->f = RHS` must invalidate EVERY
path-sensitive narrowing fact that depends on the OLD value of `s->f` — including the
deeper FieldPaths `{s,".f.g"}` / `{s,".f.g.h"}` (`s->f->g`, `s->f->g->h`) and any DerefPath
through `s->f`. After the write, `s->f` points at a DIFFERENT sub-object, so prior
refinements of `s->f->g` no longer hold.

**The bug**: the MemberExpr-LHS branch of `VisitBinaryOperator` (:632-647) handles `s->f = RHS`
by computing the EXACT FieldPath `FP = {s,".f"}` (VisitMEForFieldPath, :643) and doing only
`if (CurrStatusFP.count(FP)) CurrStatusFP[FP] = RHSKind;` (:644-645). There is:
- NO erase of deeper FieldPath entries whose key STARTS WITH `{s,".f"}` (e.g. `{s,".f.g"}`).
- NO `InvalidateDerefStatusForVar` / `InvalidateDeeperDerefStatusForPath` call.
So a `s->f->g` narrowing (key `{s,".f.g"}`, established by `if (s->f->g)`) survives `s->f = other()`,
and the post-write `*s->f->g` consults the stale NonNull and is ACCEPTED. When `other()` returns a
sub-struct whose `.g` is null → runtime Invalid read at 0x0 / SIGSEGV.

**Distinct from F84**: F84 is the VarDecl-LHS branch (:613-631), event = BASE reassign `s = other()`,
fix = add `InvalidateFieldStatusForVar(VD)` after :631. This find is a DIFFERENT branch (:632-647),
DIFFERENT event = FIELD WRITE `s->f = other()` (LHS is a MemberExpr → `getVarDeclFromExpr` returns
null → control never reaches :631; F84's fix does not touch the :632 branch). Distinct from F33
(population mis-keying; arrow form `s->f->g` has well-formed keys, no collision) and F26 (join meet).

**Root-cause-pinning controls**:
- minus-one-line baseline (probe WITHOUT the `s->f = other()` write) compiles AND runs clean
  (vg 0 errors) → the field-write line is the sole trigger.
- un-narrowed-sibling control (`*s->f->h` after the write, h never narrowed) is correctly REJECTED
  → the accept of `*s->f->g` comes specifically from the stale `{s,".f.g"}` key, not a blanket
  suppression.

**Defect class**: C7 (narrowing not invalidated across a mutation that should clear it) — the same
family as F84 but a distinct event/code-site. Sibling-asymmetry (C1) flavor: the DerefPath-store
branch (:596-611) HAS a deeper-invalidator (`InvalidateDeeperDerefStatusForPath`, :610); the
field-write branch (:632-647) lacks the equivalent for FieldPaths.

**Severity**: HIGH — pure nullability soundness FN under `-nullability-check=all`; runtime null deref;
idiomatic "narrow a nested field, then re-point an intermediate cursor field" (tree/linked-list rebind:
`if(n->left){ if(n->left->val){ use(*n->left->val); n->left = rebalance(); use(*n->left->val); } }`).

**Fix surface**: in the :632-647 field-write branch, after re-setting the exact key, erase every
`CurrStatusFP` entry whose key string is a deeper extension of `FP.second` under the same base VD,
and invalidate DerefPath facts through that field. A unified `InvalidateFieldStatusForFieldPath(FP)`
(prefix-match erase) is the clean form; the deeper-erase mirrors `InvalidateDeeperDerefStatusForPath`.

**Repro**: `/tmp/explorer_probe_R2E1.8d7Ul2.cbs` (clean compile + vg Invalid-read-0x0 / SIGSEGV).
**Baseline**: `/tmp/explorer_baseline_R2E1.eRSaaf.cbs` (probe minus the write; clean compile + clean vg).
**Probe ledger**: `/tmp/probed_R2E1.md`.

### Audited-SOUND narrowing CFG shapes this cycle (negatives — do not re-walk @ 28656aa9)
LOOP VD-reassign re-widen, LOOP DPVD-reassign re-widen, LOOP continue-guard, `||`/`&&`
short-circuit operand+else, forward-GOTO-into-narrowed-region merge, NESTED-conditional
reassign inner-if join. All re-widen / meet correctly. (switch fallthrough + do-while +
PassConditionStatusToSuccBlocks succ-order were audited SOUND in prior cycles — see
BSCNullabilityCheck.md.) The lone live narrowing gaps are now: F26 (DPVD join), F33 (FP
population collision), F70 (invert), and this E6 FP-invalidation hole.

## R3 call/addr events — 2026-05-30 (Explorer, Chain X continuation, F84/F85 follow-up)

### CALL-MAY-MUTATE does NOT invalidate ANY narrowing fact — `VisitCallExpr` (:654-668) — CONFIRMED-new

**The event the checker is BLIND to**: a function call that takes the narrowed pointer/struct
by `_Borrow` (so the callee can mutate through it) does NOT clear ANY of the three narrowing
maps (`CurrStatusVD`, `CurrStatusDPVD`, `CurrStatusFP`). `VisitCallExpr` (:654-668) checks ONLY
that NonNull params aren't passed a Nullable arg — it has ZERO interaction with the narrowing
state. So after `if (s->f) { clear(s); use(*s->f); }` where `void clear(struct S *_Borrow s){ s->f = nullptr; }`,
the `{s,"->f"}` NonNull narrowing established by `if (s->f)` SURVIVES the call, and the post-call
`*s->f` consults the stale NonNull and is ACCEPTED → runtime null deref.

**Why the unsoundness is REAL (reachability confirmed)**: `clear`'s body `s->f = nullptr;`
through a `struct S *_Borrow s` is ACCEPTED in `_Safe` (`f` is `_Nullable`, assigning nullptr is
sound). The borrow grants WRITE access to the pointee's fields. So the callee genuinely CAN null
the field the caller still believes is NonNull. The checker is intra-procedural — it does not
inspect `clear`'s body — but it ALSO fails to take the conservative position (drop narrowing on
any `_Borrow`/`_Mut` escape into a call). A sound checker must invalidate every narrowing keyed
on an argument passed by mutable borrow (or whose address escapes) at the call site.

**Distinct from F84 / F85**: F84 = VarDecl-LHS branch of `VisitBinaryOperator` (:613-631),
event = BASE reassign `s = other()`. F85 = MemberExpr-LHS branch (:632-647), event = FIELD WRITE
`s->f = other()`. BOTH are DIRECT writes the checker SEES (in `VisitBinaryOperator`) but fails to
propagate; the fix is "add an invalidate after the write". THIS find is a DIFFERENT code site
(`VisitCallExpr`, :654-668) and a DIFFERENT event class entirely — an INDIRECT mutation the checker
NEVER sees as a write (it happens inside the callee). The missing invalidation is at the CALL site,
not at a write site. F84/F85's fixes (add InvalidateFieldStatusForVar / deeper-prefix-erase in
VisitBinaryOperator) do NOT touch VisitCallExpr → this stays unsound after both are fixed.

**Defect class**: C7 (narrowing not invalidated across a mutation event) — same FAMILY as F84/F85
but the mutation is a CALL-through-borrow, not a visible assignment. Also a C6 flavor (a
conservative invalidation that should fire on a class of operations — calls taking a mutable
borrow of the narrowed object — is entirely absent: VisitCallExpr touches no narrowing map).

**Severity**: HIGH — pure nullability soundness FN under `-nullability-check=all`; runtime null
deref; the MOST idiomatic shape of all three (helper that nulls a field through a borrow:
`if (node->next) { detach(node); walk(node->next); }`).

**Fix surface**: in `VisitCallExpr`, for every argument whose type is a mutable borrow
(`*_Borrow`/`&_Mut`) of a pointer or of a struct, invalidate the matching narrowing entries
(`CurrStatusVD` for a direct pointer arg, all `CurrStatusFP`/`CurrStatusDPVD` keyed on the struct's
base VarDecl for a struct-by-borrow arg). A blanket "drop all narrowing on any non-const-borrow
escape into a call" is the conservative form.

### ADDRESS-TAKEN (`&_Mut p`) ALSO does NOT invalidate narrowing — `VisitUnaryOperator` (:674-683)
`VisitUnaryOperator` handles ONLY `UO_Deref`. `UO_AddrMut`/`UO_AddrOf`/`UO_AddrConst` are NOT
handled → taking the (mutable) address of a narrowed VD does not clear `CurrStatusVD[VD]`. This is
the SAME root family (no invalidation on an escape event); it FOLDS into the call-mutate find when
the escaped address is passed to a call (the dominant real shape). Standalone `int *_Nullable *q =
&_Mut p; *q = nullptr; use(*p)` would be a separate sub-shape but is the same missing-invalidation
class — recorded, not separately filed.

**Repro**: `/tmp/probe_R3E1_callmutate.*.cbs` (clean compile under `-nullability-check=all` + vg SIGSEGV).
**Baseline**: `/tmp/probe_R3E1_baseline.*.cbs` (same minus the `clear(s)` call → REJECTED at `*s->f`).
**Probe ledger**: `/tmp/probed_R3E1.md`.

## R4 address-taken — 2026-05-30 (Explorer, Chain X continuation, F84/F85/F87 follow-up — the LAST untraced event)

### ADDRESS-TAKEN (`&_Mut p`) of a narrowed direct-pointer VarDecl does NOT invalidate `CurrStatusVD` — `VisitUnaryOperator` (:674-683) — CONFIRMED-new

**The event the checker is BLIND to**: taking the **mutable address** of a narrowed
DIRECT-pointer VarDecl (`&_Mut p`, opcode `UO_AddrMut`, type `int *_Nullable *_Borrow`)
and nulling `p` THROUGH that address (`*pp = nullptr`, whether inside a callee or inline
through a local alias) does NOT clear the per-VD narrowing `CurrStatusVD[p]`. So after
`if (p) { *p; reset(&_Mut p); *p; }` where `void reset(int *_Nullable *_Borrow pp){ *pp = nullptr; }`,
the `CurrStatusVD[p]=NonNull` established by `if (p)` SURVIVES, and the post-escape `*p`
consults the stale NonNull (DRE arm, :390-398 → `CurrStatusVD[VD]`) and is ACCEPTED →
runtime null deref.

**Root cause site**: `VisitUnaryOperator` (:674-683) switches on `UO->getOpcode()` and
handles ONLY `UO_Deref`. `UO_AddrMut` / `UO_AddrOf` / `UO_AddrConst` fall through with NO
narrowing-map interaction — taking the (mutable) address of a narrowed VD never drops
`CurrStatusVD[VD]` (nor the DPVD/FP facts rooted at VD). A sound checker, on `&_Mut p` (a
mutable address escape that grants the holder write access to `p` itself), must invalidate
every narrowing keyed on `p`.

**Why the inline-write twin still leaks** (decouples from F84/F85): the `*pp = nullptr`
that actually nulls `p` is, to the checker, a DerefPath store on the ALIAS `pp` (key
`CurrStatusDPVD[(pp,1)]`) — it is never re-keyed back to `p`. The checker has no aliasing,
so `p` and `*pp` are unrelated facts. The ONLY place the link could be recognized is at the
`&_Mut p` expression (the moment `p`'s address escapes mutably) — exactly the unhandled
`UO_AddrMut` arm. Confirmed by the DECOUPLED probe (alias + inline `*pp=nullptr`, zero
calls): still clean-compile + SIGSEGV.

**Distinct from F84 / F85 / F87**:
- F84 = VarDecl-LHS branch of `VisitBinaryOperator` (:613-631), event = DIRECT reassign
  `p = other()` — the checker SEES the write and re-widens. The direct-write TWIN of this
  find (`p = nullptr;` inline) is CORRECTLY REJECTED → isolates the address-taken escape as
  the sole hole.
- F85 = MemberExpr-LHS branch (:632-647), event = field write `s->f = other()`.
- F87 = `VisitCallExpr` (:654-668), event = struct passed by `_Borrow`, callee writes
  `s->f = nullptr` (key `CurrStatusFP`). F87's fix (invalidate facts keyed on a `_Borrow`
  ARGUMENT'S base) targets a struct base passed by borrow; the argument here is `&_Mut p`
  (a pointer-to-pointer rvalue passed by value) and the narrowed key is `CurrStatusVD[p]`,
  NOT a field. The DECOUPLED probe proves it fires with NO call at all → the root cause is
  the `UO_AddrMut` expression in `VisitUnaryOperator`, a DIFFERENT code site than
  `VisitCallExpr`. F84/F85/F87's fixes do not touch `VisitUnaryOperator` → stays unsound
  after all three are fixed.
- F33 = `UO_AddrConstDeref`/`UO_AddrMutDeref` (`&_Mut *p`, deref-then-address) — a different
  OPCODE and a borrow-TYPE laundering issue, not narrowing-state invalidation.

**Defect class**: C7 (narrowing not invalidated across a mutation/escape event) — same
FAMILY as F84/F85/F87 but a DISTINCT site (`VisitUnaryOperator`) and event (address-taken).
Also a **C2 opcode-switch-hole** flavor: `VisitUnaryOperator` switches on `Op` and has only
the `UO_Deref` arm — the `UO_Addr*` arms are entirely absent (mirror of the F33 finding that
this same function's opcode coverage is incomplete, but for a different soundness obligation).

**Severity**: HIGH — pure nullability soundness FN under `-nullability-check=all`; runtime
null deref (vg Invalid read size 4 @ 0x0); idiomatic "narrow a pointer, hand its address to
a reset/init-out helper, keep using it" pattern (`if(p){ use(*p); reinit(&_Mut p); use(*p); }`).

**Fix surface**: in `VisitUnaryOperator`, add an arm for `UO_AddrMut` (and conservatively
`UO_AddrOf`) on a DRE → `VarDecl *VD = getVarDeclFromExpr(SubExpr)`; if present, drop
`CurrStatusVD[VD]` (re-widen to its declared nullability) AND `InvalidateDerefStatusForVar(VD)`
+ the FP facts rooted at VD — symmetric to the reassign invalidation at :631. A unified
`InvalidateAllStatusForVar(VD)` (covering VD+DPVD+FP) called on every mutable address-escape
of `VD` is the clean form; this is the same helper F84/F85/F87 want at their sites.

**Repro**: `/tmp/probe_R4E1_addrtaken.FF4FW9.cbs` (call form; clean compile + vg SIGSEGV @ 0x0).
**Repro (decoupled)**: `/tmp/probe_R4E1_alias_inline.K4rHmd.cbs` (alias + inline `*pp=nullptr`, no call).
**Asymmetry baseline (direct-write twin)**: `/tmp/probe_R4E1_directwrite_twin.NLkuml.cbs` (REJECTED).
**Asymmetry baseline (minus-escape)**: `/tmp/probe_R4E1_baseline_noreset.1hhGBA.cbs` (clean compile + clean vg).
**Probe ledger**: `/tmp/probed_R4E1.md`.

**Chain X status**: with this find, all four mutation/escape events are traced —
reassign (F84), field-write (F85), call-mutate (F87), address-taken (this). The
`event × invalidated?` table:

| event | code site | narrowing key | invalidated? | filed |
|-------|-----------|---------------|--------------|-------|
| base reassign `p=other()` | VisitBinaryOperator :613-631 | CurrStatusVD (re-widen ✓) / CurrStatusFP ✗ | partial (FP stale) | F84 |
| field write `s->f=other()` | VisitBinaryOperator :632-647 | CurrStatusFP exact ✓ / deeper FP ✗ | partial (deeper stale) | F85 |
| call mutates via `_Borrow` | VisitCallExpr :654-668 | none touched | NO | F87 |
| address-taken `&_Mut p` | VisitUnaryOperator :674-683 | none touched (only UO_Deref arm) | NO | **this (R4)** |

Chain X is now SATURATED on its four events @ 28656aa9 once this is filed. The unified
`InvalidateAllStatusForVar` fix closes all four sites.

## 2026-06-23 Explorer — post-rewrite (binary 34e6f26e / src 18111bd2) loop/short-circuit/array/borrow narrowing

Source re-read after the −256-line nullability rewrite. Function map shifted: VisitBinaryOperator
:588-647, VisitUnaryOperator :674-683, getExprPathNullability :312-428, mergeVD/FP/DPVD :844-892,
fixpoint driver runNullabilityCheck :1174-1255 (per-edge BlocksConditionStatus* applied to pred
end-status BEFORE merge), SetCFGBlocksByExpr :734-774, initStatus :813-841.

### getExprPathNullability ArraySubscriptExpr arm (:400-406) — UNPROBED
**Invariant**: a deref of `arr[i]` where `arr` has nullable element type is reported iff the element
is Nullable on the path. The arm returns the DECLARED nullability of the element type and consults
NO narrowing map. SetCFGBlocksByExpr (:734-774) records condition-narrowing only for DerefPathVD
(rooted at a UO_Deref chain over a VarDecl), VarDecl, or MemberExpr — an ArraySubscriptExpr base
matches NONE (getDerefPathVDFromExpr rejects non-UO_Deref, getVarDeclFromExpr/getMemberExprFromExpr
don't recurse into ASE). So `if (arr[0]) *arr[0];` records no narrowing AND the deref always reads
declared Nullable.
**Peers**: DRE arm (:390-398, narrowed via CurrStatusVD), MemberExpr arm (:408-420, via CurrStatusFP).
**Candidates**:
1. FP: `if (arr[0]) { use(*arr[0]); }` — element narrowed by the guard but ASE arm ignores it →
   valid narrowed code REJECTED. Asymmetry: scalar `if (p){use(*p);}` ACCEPTED. (rank 1, clean
   differential, distinct site from F26/F84.)
2. FN dual: a write `arr[0] = nullptr;` after narrowing — but no narrowing is ever recorded for ASE,
   so no FN here.
3. Pointer-typed array element returning bare Nullable even when the WHOLE array element is in fact
   non-null by init — conservative, likely intended.

### Loop back-edge / short-circuit / borrow — UNPROBED batch
4. `&&` short-circuit: `if (p && p->f) use(*p->f);` — CFG splits; second cond block must see p
   NonNull. Verify narrowing reaches the q-cond block (rank 2).
5. `_Borrow` of a nullable pointer narrowed then deref — VarDecl key is the borrow local; verify
   narrowing applies (rank 3).

### OUTCOME 2026-06-23 (Explorer, post-rewrite)
Candidate 1 (ASE arm ignores narrowing) CONFIRMED as a FP asymmetry (`if(pp[0]){*pp[0];}` REJECTED
while DerefPath twin `if(*pp){**pp;}` ACCEPTED) but **FOLDS into G07** (array-element subscript not
narrowed; same root = no narrowing recorded/consulted for ArraySubscriptExpr; G07 root-pinned to
extractDistinguishedTrackablePtr lacking the ASE case, and getExprPathNullability's ASE arm :400-406
is the read-side twin of the same gap — one fix surface). NOT a new root cause.
Candidates 4 (&&/||), 5 (_Borrow), and the whole loop/switch/goto/merge flow battery → all SOUND.
Mark candidate 1 PROBED-folded-into-G07; candidates 4/5 PROBED-shape-sound.

### 2026-06-23 Explorer — path-MERGE granularity (mergeVD/mergeFP/mergeDPVD + condition-injection) on fresh binary 34e6f26e

**Directive focus**: nullability narrowing across a CONTROL-FLOW JOIN — null-checked in ONLY ONE
branch of an if/else (or one switch case), dereffed AFTER the join where a path did NOT guarantee
non-null (FN); mirror = a _Nonnull narrowing lost at a back-edge join (FP). Defect class C7. Dedup
F65 (SafeExpr-strip narrowing), F50/F48 (isAssignmentOp opcode hole), G01 (conditional-alias);
F26 (DPVD absent-key meet) is the filed exemplar for THIS surface — distinct = path-MERGE granularity
not already filed.

**`mergeVD` / `mergeFP` / `mergeDPVD` (BSCNullabilityCheck.cpp:844-892) — RE-READ, post-rewrite**
**Invariant**: at a CFG join, each per-key nullability is the MEET (Nullable-over-NonNull) of every
predecessor's end-status, AFTER per-edge condition-narrowing is injected (runNullabilityCheck
:1204-1232 overwrites `predValX[key]=CondState.second` for the (block,pred) edge, THEN
`valX = mergeX(valX, predValX)`). An absent key on a predecessor must read as the type-default
Nullable, never "no fact".
**The asymmetry that makes F26 live**: the loop iterates `statusB` only; for a key in A but absent
in B, A's value is kept unchanged (line 850-855 `if(statusA.count(VD)) statusA[VD]=...; else
statusA[VD]=NK;` — the `else` copies B's value, but B has no entry so the key never reaches the
`else`; the key stays A's NonNull). Correct meet needs to iterate A∪B keys with Nullable default.
**Peers**: initStatus (:813-841) pre-populates `BlocksEndStatusVD[entry]`/`...FP[entry]` with Nullable
for every nullable VD/FieldDecl appearing as a top-level CFGStmt (DRE/ME); the worklist carries these
to all blocks → VD/FP absent-key branch unreachable. DPVD has NO pre-population → F26.
**Candidates** (all PROBED this cycle on 34e6f26e):
1. scalar-VD one-path (`if(c){if(!p)return;}` then `*p`) — **PROBED-SOUND**. 3-way differential:
   all-paths-check CLEAN / no-path-check REJECT / one-path-check REJECT. mergeVD takes Nullable from
   the unchecked pred (p seeded Nullable at entry). /tmp/explorer_baseline_all.31mQXv.cbs,
   /tmp/explorer_baseline_none.QfL4G6.cbs, /tmp/explorer_probe.cccCC8.cbs.
2. VD absent-key via "p never referenced bare" (only in `if(p==nullptr)`+`*p`) — **PROBED-SOUND**.
   Dump confirms p is Nullable at entry in EVERY block (initStatus pre-populates despite no bare DRE
   — the `p` inside `if(p==nullptr)` and `*p` under setAllAlwaysAdd full-linearization surfaces a
   top-level DRE/ME CFGStmt that seeds it). The notes' "only top-level DRE" explanation is
   INCOMPLETE — another seeding path exists — but the protection holds. /tmp/explorer_probe_vd2.*.cbs.
3. flat FP one-path (`s->_Nonnull; s->f _Nullable; if(c){if(!s->f)return;} *s->f`) — **PROBED-SOUND**
   (under -nullability-check=all; NOT _Safe, which masks via the raw-`*`-forbidden zone gate).
   3-way: all CLEAN / none REJECT / one REJECT. FP key {s,"->f"} seeded Nullable at entry.
   /tmp/explorer_probe_fp3.qqm9kW.cbs + baselines.
4. deeper FP one-path (`s->f->g`, both intermediate + leaf _Nullable) — **PROBED-SOUND**. Dump
   confirms BOTH `s..f` and `s..f.g` seeded Nullable at entry and stay Nullable through the join
   (mergeFP sound on deeper paths). /tmp/explorer_probe_deep.u8z3ha.cbs.
5. DPVD one-path = **F26 (UNCHANGED on 34e6f26e)** — F26 repro still compiles clean (exit 0, FN).
   The rewrite did NOT touch mergeDPVD. Dump confirms join block has `*p: NonNull` (the bug).
6. loop back-edge FP (narrow p BEFORE loop, `*p` after) — **PROBED-SOUND** (narrowing survives
   back-edge; control no-narrow REJECT). /tmp/explorer_probe_loop.*.cbs.
7. loop body narrow + back-edge DROP (narrow p INSIDE `while` body, `*p` after) — **PROBED-SOUND**
   (zero-iteration path correctly REJECTs; back-edge drops the in-body narrowing).
   /tmp/explorer_probe_lb.*.cbs.
8. `&&` short-circuit join (`if(c && p){...}` then `*p`) — **PROBED-SOUND** (c-false path didn't
   evaluate p → after-join `*p` REJECT). Re-confirms 2026-06-23 candidate 4 on the fresh binary.
   /tmp/explorer_probe_sc.*.cbs.

**OUTCOME**: no-new-pattern. The path-MERGE granularity surface (mergeVD/mergeFP/mergeDPVD +
condition-injection at :1204-1232) is SOUND across scalar-VD / flat-FP / deeper-FP / loop-back-edge
(both directions) / &&-short-circuit on fresh binary 34e6f26e. The ONLY live merge unsoundness is
F26 (DPVD absent-key), UNCHANGED by the rewrite. The `initStatus` pre-population protects VD/FP at
every depth probed (including the deeper-FP case the prior cycle flagged as "rank 2"). Recommend
next: the path-MERGE surface is saturated-sound @ 34e6f26e — pivot OFF nullability merges. The one
untested adjacent cell is whether condition-injection (:1207-1209) can inject NonNull for a pred
where the pointer is genuinely Nullable when a block has MULTIPLE in-edges from the SAME condition
block (e.g. a goto/diamond re-entering a block reachable from both the true and false arm of one
condition) — but this requires a construct where one condition block is a predecessor via two
distinct edges, which the in-scope if/while/switch/&& CFG shapes do not produce; likely
unreachable.

## scalar narrowing invalidation across loop back-edge — probe 2026-06-24
**Invariant**: a `p` narrowed nonnull before a loop, reassigned to Nullable in the body, must have its
narrowing INVALIDATED on the back-edge so a top-of-loop `*p` (reached on iter 2+ with p nullable) is flagged.
**Peers**: F84 (FieldPath not invalidated on reassign — the field analog), reassign-invalidation, loop merge.
**Candidates**: 1. **`if(p){ while(c){ *p=1; p=getnull(); } }` — top-of-loop *p flagged on back-edge (sound)
vs narrowing persists (FN deref nullable)** UNPROBED ⭐. 2. narrowing set INSIDE loop. 3. do-while variant.

## mergeVD / mergeFP / mergeDPVD (BSCNullabilityCheck.cpp:849-897) — CFG join of nullability state (2026-06-26 S3)
INVARIANT: the join of nullability across CFG predecessors is "Nullable wins over NonNull" — a VD/FieldPath/DerefPath
is NonNull at a join only if NonNull on ALL incoming paths. Implemented by iterating statusB and setting
result[k]=Nullable iff statusB[k]==Nullable, else keeping statusA[k]; keys only in B are inserted; `empty()`→other.
PEERS: the ENTRY seeding of the status maps (does runOn seed every declared-_Nullable param/field as Nullable, or
are maps lazy/narrowing-only?); runOnBlock (:899); the narrowing transfer (VisitBinaryOperator `if(p)`); recently
touched by dfa23b83 (De Morgan negated conditions) + ced6364 (UO_Deref FieldPath keying).
CANDIDATES:
1. (C5 merge hole, UNPROBED, top) A key narrowed to NonNull on predecessor A but ABSENT in predecessor B (never
   narrowed on B) → loop over B never visits it → result KEEPS NonNull. UNSOUND iff "absent in B" semantically means
   declared-Nullable (lazy map). Probe: `if(c){ if(!p) return; }` then use p — c-false path doesn't narrow p; if the
   join treats p as NonNull, a null p (c-false) is deref'd unguarded. [needs compiler — probe after rebuild]
2. (asymmetry, UNPROBED) merge only iterates statusB's keys; a stale NonNull in A whose true B-state is Nullable-by-
   absence is never downgraded → order/path-set dependent result.
3. (DerefPath, UNPROBED) mergeDPVD same pattern for `*p` deref-paths — narrowed-on-one-side deref-path kept NonNull.
NOTE: direction ("Nullable over NonNull") is correct; the risk is the ABSENT-key default, contingent on entry-seeding.

### RESOLUTION (2026-06-26 S3): candidate 1/2 merge-hole — LIKELY SOUND (entry-seeding closes it)
initStatus (BSCNullabilityCheck.cpp:818) seeds EVERY referenced declared-_Nullable VD (:830-832) and field
(:835-839) as Nullable in BlocksEndStatusVD/FP[entry]. So a declared-Nullable pointer is present (=Nullable) on
every path that didn't narrow it → the merge's "iterate statusB, Nullable wins" sees an explicit Nullable on the
unnarrowed predecessor → join = Nullable. The "absent in B" case does NOT arise for referenced declared-Nullable
pointers (the only ones deref-relevant). Dynamically-nullable (declared NonNull, assigned null on one path) is also
handled: the explicit Nullable entry propagates via the count()/else insert. Merge SOUND given seeding. Confirmatory
probe (if(c){if(!p)return;} then use p) pending post-rebuild. Reassign-invalidation is the separate F84 surface.
