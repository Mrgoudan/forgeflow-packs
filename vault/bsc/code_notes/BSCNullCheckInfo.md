
## extractDistinguishedTrackablePtr (BSCNullCheckInfo.cpp:170-?) — SafeExpr-strip — RESOLVED-by-implication 2026-06-08

**Resolution**: the ~20 passing narrowing probes (if(p){*p}, loop/ternary/&&/||/field narrowing) all REQUIRE correctly identifying the tracked pointer through the compiler-inserted SafeExpr wrapper in _Safe context. They all narrow correctly → the SafeExpr-strip works. No separate probe needed; validated by implication.


**Invariant**: when called on a condition expression, the function returns the "trackable pointer" the condition discriminates on. Wrapper AST kinds (paren, cast, comma) that don't change the trackable identity are peeled to find the underlying pointer.

**Peers**: `CheckMoveVarMemoryLeak` (F62 site) — also calls `IgnoreParenCasts` which doesn't strip BSC `SafeExpr`. Same SafeExpr-strip family.

**Candidates**:
1. **Composition** — if `extractDistinguishedTrackablePtr` uses `IgnoreParenCasts`, then a condition `if (_Safe(p))` would have its top-level `SafeExpr` survive the strip → trackable pointer extraction fails → narrowing doesn't fire → inside the if body, `*p` is still treated as `_Nullable` → false-positive deref diagnostic.
2. **Symmetry** — if narrowing is GAINED through `_Safe(...)` even though the function doesn't peel SafeExpr (i.e. some other peel-step handles it), that's already correct. If lost, that's the bug.

Top: candidate #1. Probe: `if (_Safe(p))` with `*p` inside the true branch; expect false positive if SafeExpr-strip is missing.

## NullCheckInfo::operator|= / operator&= (BSCNullCheckInfo.cpp:236-334) — READ 2026-05-22

**Invariant**: `&&` is union over null/present sets (both arms contribute narrowings); `||` is intersection (only commonly-narrowed exprs survive). Triviality (ConstTrue/ConstFalse) short-circuits set ops.

**Peers**:
- `NullCheckInfo::invert` (line 227-234) — swaps null↔present sets for negation.
- `obliviateInfeasible` (line 348) — handles contradictions (same expr in both sets).
- `extractAndInsert` (line 336) — entry point for building info.

**Candidates**:
1. **`if (p && !p)` contradiction**: operator&= unions present={p} ∪ null={p}, leaving p in BOTH sets. PROBED — body is unreachable at runtime so no soundness bug; analyzer accepts deref because present={p}. CORRECT but precision-wasteful.
2. **`if (p || !p)` tautology**: operator|= intersects {p} ∩ {} = {}. PROBED — analyzer correctly does NOT narrow; deref diag fires. CORRECT.
3. **`if (p || p)` redundant**: operator|= intersects {p} ∩ {p} = {p}. p narrowed to present in body. PROBED — accepts deref correctly.

Top: candidate #1 (contradiction). Probed — analyzer is consistent (no soundness violation), no bug.

## NullCheckInfo::invert() (BSCNullCheckInfo.cpp:227-234) — PROBED-confirmed-F70 (2026-05-29)
**Invariant**: negation of a condition swaps which exprs are known-null vs
known-present. **BUG**: it does an unconditional `nullCheckedExprs.swap(presentCheckedExprs)`,
correct ONLY for a single comparison. For a compound condition already combined
by operator&=/|= (init :198), the swap is a broken De Morgan: `!(p==nullptr && cond)`
→ init gives {null:{p}} → invert swaps → {present:{p}} → p wrongly NonNull → `*p`
accepted → runtime SIGSEGV. **F70 (HIGH, filed)**. Controls: simple `!(p==nullptr)`
correct; bare `*p` rejected. `operator|=` (:286) comment already warns De Morgan
is unsafe — invert() ignores that. repro/F70_nullcheck_invert_compound_demorgan.cbs.

## getTrackablePtr / extractDistinguishedTrackablePtr (BSCNullCheckInfo.cpp:100-195) — 2026-05-29
**Invariant**: classify a condition sub-expr into (trackable ptr, NullNess).
Forms: bare ptr lvalue / `*p` → present; `p==nullptr`→null, `p!=nullptr`→present
(via isEqualityOp + isNullExpr, reversed operands handled :187); comma→RHS;
assignment `p=foo()`→LHS present; `!p` via init() UO_LNot→invert (single-expr, sound).
Rejects: non-lvalue, non-pointer, volatile/atomic, containsArrayAccess (subscript).
**Candidates**: 1. `if (p != q)` (both ptrs, neither null) wrongly narrowing p — guarded by
XNOR check (:183, both-non-null → nullptr). 2. relational `p > nullptr` — not isEqualityOp → untracked (safe). 3. `!p` single-negation → invert sound (vs F70 compound).
**Probe outcome (2026-05-29): PROBED-SOUND.** `if(!p)return;*p`→clean; `if(p==0)return;*p`→clean (0 as null const); `if(p!=q)*p`→correctly REJECTED (no FN, XNOR :183 stops p!=q narrowing); `nullptr==p` reversed→clean. Extraction robust. BSCNullCheckInfo now characterized: extraction/simple-negation/short-circuit all SOUND; only compound-negation via invert() (F70) is buggy.

## init() logical-op recursion REACHABILITY + De-Morgan-over-OR / comma-defeat (2026-05-29 bsc-explorer PRODUCER chain)
**Question**: can a condition reach `init`'s `&=`/`|=`/invert recursion and PRODUCE a spurious
`present`(NonNull) fact via a path DISTINCT from F70's `!(p==null && cond)`?
**Findings (reachability map)**:
 - CFG ALWAYS splits top-level `&&`/`||` → `getLastCondition()` is a leaf → init's `&&`/`||` recursion
   is DEAD for the nullability pass EXCEPT under `!` (which does NOT split). So init's logical recursion
   is reached ONLY through `UO_LNot`. That is exactly the F70 surface.
 - The dangerous direction (null→present via invert) requires the inner combinator to put a ptr in the
   NULL set. Only `p==nullptr` does that. `!(p==null && cond)`=F70; `!(p==null && q==null)` (both
   null-checks) → `&=` UNIONs null={p,q} → invert → present={p,q} → FOLD-F70 (same root, 2 ptrs).
 - `!(...||...)`: `|=` INTERSECTS; two distinct null-checks intersect to {} → invert empty → CONSERVATIVE
   (P1 `!(c || p==null)` correctly does NOT narrow → false-positive only, safe). So OR-under-`!` is sound.
 - COMMA defeats CFG-split AND defeats narrowing: `if((0,(p!=null && q!=null)))` → init gets the comma,
   not a logical op → extractAndInsert → extractDistinguishedTrackablePtr(comma) recurses RHS to the
   `&&`, which extractDistinguishedTrackablePtr does NOT handle → returns null → NO narrowing
   → false-positive (P3). Comma is always the SAFE direction (kills facts, never fabricates a present).
**Conclusion**: the producer's combinator (init/&=/|=/invert) has NO new soundness hole beyond F70.
Every spurious-present route reduces to `!`-over-`&&`-with-`==nullptr` (F70). OR-under-`!` and
comma-wrapping are conservative.

## VisitMEForFieldPath FieldPath-collision (producer key) — FOLDED-F33 (2026-05-29)
**Probed the producer key-extraction collision**: `if((*pp).f != nullptr) return *(*qq).f;` in a
NON-_Safe fn (raw deref allowed; nullability diag fires outside the safe zone too). Compiles clean
under -nullability-check=all; runtime SIGSEGV (exit 139) when qq->f is NULL. Control `*(*qq).f` with
NO narrowing → correctly REJECTED, so the accept-in-branch is the collision. This is EXACTLY F33
(VisitMEForFieldPath :149-163 omits UnaryOperator base → FP.first stays nullptr → all `(*X).f`
collapse to key (nullptr,".f")). NOT new. /tmp/explorer_repro.PBJjvD.cbs, baseline AXFXnL.
**Sibling**: subscript base `pp[0].f` does NOT collide — `containsArrayAccess`/`getTrackablePtr` reject
subscript-containing exprs so `(pp[0].f != nullptr)` produces NO narrowing → `*qq[0].f` correctly
rejected (P7). So F33 is specifically the deref-base (UnaryOperator) case; subscript base is conservative.

## R2 merge-operators — operator|=/&= lattice-direction proof + invalidation merge (2026-05-30 Explorer, NO-NEW)

**Target**: a soundness gap in `NullCheckInfo::operator&=` (:303) / `operator|=` (:333)
SET-DIRECTION, or the CFG-join nullability merge, where a pointer is kept NonNull on a path
that didn't establish it. Binary 28656aa9. Flag `-Xclang -nullability-check=all`. Ledger /tmp/probed_R2E5.md.

**Lattice-direction proof (the operators are SOUND — no change needed)**: `&=`/`|=` are NOT
CFG-join merges; they are the CONDITION-CONSTRUCTION combinators for the two operands of a single
`&&`/`||`. `&=` UNIONs BOTH `nullCheckedExprs` and `presentCheckedExprs` (correct — on the `A && B`
TRUE path both A's and B's facts hold). `|=` INTERSECTS BOTH sets (correct — on the `A || B` TRUE
path only facts common to both operands are guaranteed). Neither over-claims a present(NonNull): every
present member came from a sub-condition that genuinely establishes it on its own true path. The "non-
null set must INTERSECT at a join" rule applies to the CFG-JOIN merge (mergeVD/FP/DPVD), which IS
Nullable-over-NonNull (sound intersection); the only hole there is the already-filed F26.

**Probes (8-budget, all SOUND or FOLD-F26)**:
1. R2-A nested-then `if(c){if(p!=null){*p}}else{}` then `*p` → REJECTED. SOUND (merge drops narrowing).
2. R2-B p only as `p!=null`/`*p` (never bare DRE) → REJECTED. SOUND — full CFG linearization makes the
   inner `p` its own top-level CFGStmt DRE so initStatus pre-populates it → mergeVD absent-branch dead.
3. R2-C/C2 field reassign `s->f=r`(Nullable) after `if(s->f!=null){*s->f}` then `*s->f` → REJECTED.
   SOUND (MemberExpr-assign updates CurrStatusFP[FP]=Nullable; count(FP) true). Distinct-from-F84 sound.
4. R2-D `if(*pp!=null && *qq!=null){**pp+**qq}` two deref-narrows → ACCEPTED both. SOUND (CFG splits &&;
   each leaf injects one DPVD pair on its own edge; single-pair-per-edge never overwrites).
5. R2-E loop body narrow + back-edge reassign-to-Nullable, head `*p` → REJECTED. SOUND (head fixpoint
   merge takes Nullable from entry path over body NonNull).
6. R2-F `if(*pp!=null){if(c){pp=qq;}return **pp;}` — root pp reassigned on c-true (clears (pp,1)); merge
   with c-false (pp,1)=NonNull → ACCEPTED; **valgrind SIGSEGV / Invalid read at 0x0**. **FOLDED-F26**:
   mergeDPVD absent-key meet (`if(statusA.empty())return statusB`+absent-branch keeps NonNull,
   BSCNullabilityCheck.cpp:879-889) reached via reassignment-invalidation instead of sibling-never-
   establishing. SAME root cause + SAME one-line fix (symmetric meet). NOT new. /tmp/explorer_vg.LUzsnW.cbs.
7. R2-G `if(p!=null && q!=null){...}` then `*q` short-circuit && false-path → REJECTED. SOUND (no stale q).
8. R2-H nested FieldPath `s->a.f` narrowed in one sibling, post-merge `*s->a.f` → REJECTED. SOUND —
   nested MemberExpr is a top-level CFGStmt under full linearization → initStatus pre-populates → mergeFP
   absent-branch dead (confirms F26 is DPVD-only; nested FP gets same protection as flat FP).

**Conclusion**: operators |=/&= SOUND (union for &&, intersect for ||, over both sets). CFG-join meet is
sound Nullable-over-NonNull. Only soundness hole = F26 (mergeDPVD absent-key); R2-F folds into it via the
reassignment-invalidation entry path. mergeVD/FP protected by initStatus pre-population (robust under full
CFG linearization). **SATURATED @ 28656aa9 for the merge/combine/invalidation soundness-meet question.**
Reopen-if a commit touches mergeDPVD/FP/VD, InvalidateDeeperDerefStatusForPath, initStatus, or the
operator|=/&= set-direction. Residual: VisitBinaryOperator MemberExpr-assign branch (:631-642) updates
CurrStatusFP[FP] only `if count(FP)` and calls NO Invalidate*ForVar — F84 covers base-reassign; the
field-self-reassign half is sound only because count(FP) was true (conservative-default otherwise).

## operator&= / operator|= (BSCNullCheckInfo.cpp:236-334) — condition narrowing-set combine

**Invariant**: combining the narrowing facts of a compound condition — `&&` (`&=`)
UNIONS the present/null checked-expr sets (if EITHER branch checks an expr, the
whole `&&` checks it); `||` (`|=`) INTERSECTS them (only when BOTH branches check
an expr does the `||` check it); ConstTrue/ConstFalse triviality is const-folded
(`&&`+ConstFalse→clear; `||`+ConstTrue→clear).
**Peers**: `invert()` (:227, negation swaps present↔null for else-branch),
`init()` (:198, builds from Cond), `obliviateInfeasible()` (:348),
`semanticUnion`/`semanticIntersect` (set ops using ctx SEMANTIC expr-equality).
**Candidates**:
1. **`||` must NOT narrow either operand: `if(p||q) *p;` must REJECT — PROBED-SOUND**.
   `int*_Borrow _Nullable` p,q: `if(p||q) *p` correctly REJECTED, `if(p&&q) *p`
   correctly ACCEPTED (probes/null_or_combine_no_narrow.cbs). Combine arithmetic sound.
2. **`semanticUnion`/`semanticIntersect` semantic-equality** could mis-identify two
   different exprs as equal (alias to the F42 path-identity / F18/F48/F50 family).
3. **`invert()` × combine for else-branch**: `if(p&&q){}else{...}` — else = `!p||!q`,
   neither narrowed; verify no spurious narrowing leaks into the else. RESOLVED-SOUND 2026-06-08: `if(p||q){return}; *p` → deref REJECTED (p null in else, no spurious narrowing). /tmp/ore.cbs.

### semanticEqual / getSemanticExprID (BSCNullCheckInfo.cpp:33-46) — probing candidate 2
**Invariant**: two checked-expr entries are "the same tracked location" iff
`getSemanticExprID(a)==getSemanticExprID(b)`. Soundness REQUIRES distinct runtime
locations get distinct IDs — else narrowing one launders to the other.
**Probe target**: same field, DIFFERENT base object (`s1->f` vs `s2->f`). If the
ID drops the base, `if(s1->f) *s2->f` is wrongly accepted = HIGH FN.
Note `containsArrayAccess` (:77) excludes `[]`/`*(p±x)` exprs from tracking
(closes the array-index-aliasing hole — so the live risk is field-base identity).

## &&-chain narrowing breadth (limit-sweep) — probing
**Invariant**: `if(p0 && p1 && … && pN)` narrows ALL N pointers (operator&= unions
the present-set per arm); the narrowed-set must not cap at a fixed size.
**Peers**: operator&= (sound, cycle 17/semantic-eq), F96 (depth cap, ownership).
**Candidates**:
1. **&&-chain of N nullable derefs → all narrowed, or cap past K (FP)? — sweeping**.
2. ||-chain breadth (intersect). RESOLVED-SOUND 2026-06-08: `if(p||q){*p}` → REJECTED (|| narrows neither). /tmp/or.cbs.
3. mixed &&/|| breadth. RESOLVED-SOUND 2026-06-08: `if((p&&q)||r){*p}` → REJECTED (p not guaranteed on r-path; no over-narrow). && narrows both (and.cbs). /tmp/mix.cbs.

## negated-comparison narrowing direction — probing
**Invariant**: `!(p==nullptr)` ≡ p!=null → narrow (deref OK); `!(p!=nullptr)` ≡
p==null → deref must REJECT (p is null in-branch). Wrong direction = FN.
**Peers**: F70 (compound-condition invert), getTrackablePtr/invert.
**Candidates**:
1. **`if(!(p!=nullptr)) *p` (p==null in branch) → REJECT? — probing** (FN if accepted).
2. `if(!(p==nullptr)) *p` → ACCEPT (control).

## operator&= / operator|= (compound-condition lattice, :236/:286) — candidates 2026-06-17
INVARIANT: `A&&B` → UNION of checked exprs (either branch checking p ⇒ whole checks p); `A||B` → INTERSECTION (only if BOTH check p). Then-branch narrows present-checked to non-null.
Candidates:
1. [merge-hole C5] `if(p||q)` then-branch passing p to _Nonnull → **PROBED-SOUND 2026-06-25**: correctly REJECTED "cannot pass nullable pointer argument" (p NOT narrowed; only the disjunction known). Control `if(p)` correctly narrows (rc=0). No FN.
2. [symmetry] `if(p&&q)` then: both non-null (union) → clean; FP if rejected. UNPROBED
3. [invert/DeMorgan] `if(!(p&&q)){}else{...}` else-branch both non-null. UNPROBED

## NullCheckInfo operator&= / operator|= merge (BSCNullCheckInfo.cpp:236-330) — read 2026-06-24
**Invariant**: `&&`(operator&=) UNIONs both branches' null/present checked-expr sets (both true → both
narrowings); `||`(operator|=) INTERSECTs (only common narrowing). ConstTrue/ConstFalse short-circuit
handling symmetric (either-branch checked at top). semanticUnion/Intersect on null+present sets.
**Peers**: mergeDPVD (F26, the CFG-block meet — distinct function), semanticUnion, getExprPathNullability.
**Candidates**: 1. order-asymmetry `p&&q` vs `q&&p` — **PROBED-SOUND 2026-06-25**: both orders narrow p AND q non-null identically (rc=0 both); `if(p||q)else` narrows both to null (rejected). Boolean-narrowing sound, order-independent.
2. mixed `p && !q` present/null set handling. 3. nested `(p||q)&&r`.

## NullCheckInfo::init + operator&=/|= (BSCNullCheckInfo.cpp:199-300) — De Morgan negated narrowing (adjacent-fix re-probe of dfa23b83, 2026-06-26)
INVARIANT: for a (possibly negated) condition, build presentCheckedExprs (proven NON-null when cond true) + null
CheckedExprs (proven null) such that the then/else branch narrowing is SOUND (never mark a pointer non-null unless
guaranteed). De Morgan via `Negate` flag: UO_LNot flips Negate (:208); logical BO uses `(opcode==BO_LAnd)!=Negate`
to pick &= vs |= (:221). |= explicitly does NOT reuse De Morgan at the set level (:283).
PEERS: extractAndInsert, obliviateInfeasible (:216/219), the narrowing transfer that consumes present/nullChecked.
CANDIDATES:
1. (over-narrow in negated-AND then-branch) `if(!(p&&q)) sink(p)` — PROBED-SOUND (see F122 entry below:
   then-branch correctly narrows nothing; the defect is the else/fall-through FP = F122).
2. (AND else-branch) `if(p&&q){}else sink(p)` — else=!(p&&q), p not guaranteed; framework-negated path.
   Covered by F122 root analysis: the else swap has an EMPTY set to narrow → conservative reject = no FN.
3. (mixed nesting) `if(!(p || !q)) ...` — PROBED-SOUND 2026-07-06 both directions
   (probes/negated_or_mixed_demorgan_narrow.cbs): then-branch ≡ (!p && q); need(q) ACCEPTED (q narrowed,
   no FP) + need(p) REJECTED (no FN); identical to explicit twin `!p && q`. Asymmetry vs F122 explained:
   `||` under Negate → operator&= UNION (preserves both operands' sets) so nested-negation-through-OR
   narrows fine; only `&&` under Negate → operator|= INTERSECT collapses {p}∩{q}={} (= F122). The De Morgan
   composition edge is sound everywhere except the already-filed F122 arm.

## FILED F122 (2026-06-29) — negated-AND guard not narrowed (false positive)
**PROBED-confirmed-F122.** `if(!(p && q)) return; need(p);` REJECTED "cannot pass nullable pointer argument"
(p IS nonnull after the guard) while the De Morgan twin `if(!p || !q) return; need(p);` ACCEPTED — proves
implementation inconsistency, not a conservative limitation. Single-var `!p` guard + non-negated `p&&q` then-branch
both narrow; only the negated-PARENTHESIZED-AND fails. Root: init() routes `!(p&&q)` via UO_LNot→BO_LAnd-under-Negate
→operator|= intersect (:221-224,327-328) collapsing {p}∩{q}={}; the else/fall-through swap (BSCNullabilityCheck.cpp:806-812)
then has nothing to narrow. FIRST nullability FALSE POSITIVE (F18/F48/F50/F84/F92 are all soundness FNs). Repro:
repro/F122_negated_and_guard_not_narrowed_false_positive.cbs. The earlier note candidate "negated-AND then-branch over-narrow (FN)"
PROBED-SOUND (then-branch correctly narrows nothing); the defect is the ELSE/fall-through under-narrow (FP).

## NullCheckInfo::init + operator|=/&= (BSCNullCheckInfo.cpp:198/282/232) — condition null-check extraction & combine (F122 root)
- **Invariant**: `init(Cond, Negate)` builds the set of null-checked/present-checked exprs for a branch condition; `!` flips Negate (:208), `&&`/`||` combine via operator&=/|= with De Morgan applied through Negate (:221 `(BO_LAnd)!=Negate` picks the combine).
- **Peers**: BSCNullabilityCheck narrowing (consumes the sets), getExprPathNullability, F122 (filed FP), F65 (SafeExpr-strip).
- **Candidates**: (1) **F122 root (source+twin confirmed)**: `!(p&&q)` (UnaryLNot wrapping BO_LAnd) does NOT narrow the false-branch present-set the same as the explicit De Morgan twin `!p||!q` (BO_LOr of UnaryLNots), though :221 SHOULD make them equivalent — a divergence in the negated-AND init/combine path. F122 filed; re-validated genuine FP (twin `!p||!q` accepted rc=0, `!(p&&q)` rejected rc=1, identical semantics). (2) operator|= ConstTrue/ConstFalse short-circuits (:288-) — sound for the documented cases. (3) the present-vs-null set extraction (extractAndInsert:332) for nested negations.
