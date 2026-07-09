# BSCIRBuilder.cpp (clang/lib/Analysis/BSC) — IR lowering for the init-analysis (BSCIR-based)

## lowerStmt dispatch (:257-346) — read 2026-06-25
INVARIANT: every reachable statement is lowered into the BSCIR so the init-analysis (BSCIRInitAnalysis) sees its
control-flow + def/use effects; an unhandled Stmt kind falls to `emit(createNop)` → INVISIBLE to init-analysis
(the F98 class: braceless-switch bodies were Nop'd → init-blind, now FIXED).
HANDLED (in-scope, complete): SafeStmt, CompoundStmt, IfStmt, WhileStmt, ForStmt, DoStmt, SwitchStmt, BreakStmt,
ContinueStmt, GotoStmt, LabelStmt, ReturnStmt, DeclStmt, NullStmt(skip), AttributedStmt(recurse), Expr(Visit).
CaseStmt/DefaultStmt handled INSIDE lowerSwitchStmt (probe-confirmed: switch fall-through + braceless init both
flagged → case bodies lowered).
NOP-FALLBACK kinds (all OUT OF SCOPE): GCCAsmStmt/MSAsmStmt (asm), IndirectGotoStmt (computed `goto *p` = GNU
&&label, OOS), CXXTryStmt/CXXForRangeStmt (C++), CapturedStmt/CoroutineBodyStmt (coroutines OOS), StmtExpr (GNU OOS).
CANDIDATES (all resolved):
1. CaseStmt leaking to Nop via lowerCompoundStmt over a switch body — SHAPE-REJECTED: lowerSwitchStmt intercepts;
   switch fall-through/braceless init probes flag correctly (2026-06-25).
2. GCCAsmStmt output-operand init missed → FP — OOS (asm) + FP-not-FN; not pursued.
3. IndirectGotoStmt control-flow missed → init FN — OOS (GNU computed goto / &&label).
lowerStmt dispatch SOUND for in-scope statements; Nop-fallback only hits OOS kinds.

## Expression Visit dispatch (BSCIRBuilder.cpp, StmtVisitor: VisitDeclRefExpr/VisitCallExpr/... :728+; fallback VisitStmt :1313) — read 2026-06-25
INVARIANT: every reachable expression is lowered to BSCIR Operands/Statements so init-analysis sees its def/use;
an unhandled Expr kind falls to VisitStmt (:1313) which emits Nop + returns a void Constant WITHOUT lowering its
children → that expr's operands' def/use are INVISIBLE to init-analysis (the F88/F90/F98 C3 class, expr side).
HANDLED (in-scope, comprehensive): all literals (Integer/Floating/Character/String/Predefined/CXXNullPtr/GNUNull),
DeclRef, Member, ArraySubscript, Call, Cast (CStyle+Implicit), Binary, CompoundAssign, Conditional, Unary,
UnaryExprOrTypeTrait (sizeof), Paren, InitList, CompoundLiteral, ImplicitValueInit, Atomic, VAArg, Safe.
NOP-FALLBACK (VisitStmt, children NOT lowered) kinds = ALL OUT OF SCOPE: StmtExpr `({...})`, GenericSelectionExpr
(_Generic), ChooseExpr (__builtin_choose_expr), BinaryConditionalOperator (`?:` GNU), vector/complex/block exprs.
CANDIDATE (OOS, not pursued): an OOS wrapper (e.g. StmtExpr `({ x=5; x; })`) hiding an in-scope init → VisitStmt
Nops it → init missed (FP/FN). Reachable ONLY via OOS GNU constructs (user-excluded). In-scope expr coverage SOUND.

## lowerStmt + expr Visit dispatch coverage (BSCIRBuilder.cpp:257/1313, 2026-06-26 re-read)
INVARIANT: every init/move-relevant stmt+expr must lower to IR so init-analysis sees the init/move; unhandled kinds
fall to createNop (:345 stmt / :1315 expr) = init-analysis BLIND (the F52/F98 class).
COVERAGE: stmt dispatch complete for in-scope C (Safe/Compound/If/While/For/Do/Switch/Break/Continue/Goto/Label/Return/
Decl/Null/Attributed/Expr); expr dispatch handles BinaryOperator/Unary/Call/Cast/ImplicitCast/CompoundLiteral/InitList/
CompoundAssign/Conditional/ParenExpr(:1250)/CXXNullPtr + lvalue DeclRef/Member/ArraySubscript. Fallthrough → OOS/edge.
CANDIDATES:
1. (VAArgExpr owned) — PROBED-SOUND 2026-07-06 (probes/vaarg_owned_leak_untracked.cbs). Premise was wrong twice:
   (a) VAArgExpr does NOT fall through — VisitVAArgExpr (:1146) lowers it to an opaque constant temp; (b) even
   though BSCOwnership.cpp has no VAArgExpr arm, `int *_Owned p = va_arg(ap, int*_Owned);` is a DeclStmt with an
   owned-typed init → p set Owned regardless of initializer kind. All three directions correct in an UNSAFE
   variadic fn (variadic forbidden in _Safe): drop-without-consume → "memory leak of value: p"; consume → clean;
   double-consume → "use of moved value: p". Leak check confirmed active in unsafe functions.
2. (AtomicExpr owned, edge) `__c11_atomic_exchange` on owned → Nop → move untracked. Edge/unusual.
3. (stmt fallthrough) only IndirectGoto/asm (GNU/edge) → Nop; no in-scope stmt gap.

## IRBuilder StmtVisitor dispatch coverage (BSCIRBuilder.cpp, 2026-06-27 Mode-1 — bounds the audit surface)
28 Visit* methods: ArraySubscript/Atomic/Binary/Call/Cast/CharLit/CompoundAssign/CompoundLiteral/Conditional/CXXNullPtr/
DeclRef/DeclStmt/FloatLit/GNUNull/ImplicitCast/ImplicitValueInit/InitList/IntLit/Member/Paren/Predefined/Return/SafeExpr/
Stmt(default)/StringLit/UnaryExprOrTypeTrait/UnaryOperator/VAArg. CStyleCast handled via base VisitCastExpr (no separate
gap). IN-SCOPE COVERAGE COMPLETE EXCEPT **BinaryConditionalOperator (GNU `?:`) = F120 (filed)** — VisitConditionalOperator
(:1258) handles the standard ternary, but no VisitBinaryConditionalOperator. Other missing visitors are OOS: VisitStmtExpr(0),
VisitChooseExpr(0, __builtin_choose_expr), GenericSelection(_Generic). CONCLUSION: no NEW in-scope IRBuilder-dispatch hole
beyond F120. Bounds GLM's audit oracle (it keeps re-finding F120 because that IS the one gap).

## DISPATCH-COVERAGE ORACLE — full sweep across 3 analyzer dispatches (2026-06-29)
Systematic run of the dispatch-coverage finder (the method behind F120/F111). Key discriminator =
what the DEFAULT `VisitStmt` does when an expr kind has no dedicated visitor:

| Dispatch | default VisitStmt | gap exploitable? | reachable in-scope gaps |
|---|---|---|---|
| **BSCIRBuilder** lowerExpr (Visit*, :728-1316) | → `createNop` (DROPS tracking, :1313) | **YES** | `BinaryConditionalOperator` (GNU `?:`) = **F120 filed**. C++ wrapper exprs (`ExprWithCleanups`/`ConstantExpr`/`MaterializeTemporaryExpr`/`CXXBindTemporaryExpr`) are SHAPE-REJECTED — C++-only AST nodes, never produced for C-based BSC (AST-dump confirms only `ConditionalOperator` appears). |
| **BSCOwnership** TransferFunctions (:2032+) + `runOnBlock` isa-filter (:2647) | → Nop; runOnBlock routes only {DeclStmt, CallExpr, assign-BinaryOperator, inc/dec-UnaryOperator, ReturnStmt} | **YES** | `runOnBlock` filter = **F111 filed** (bare `*p;`/`p==p;`/`(void)*p;` use-after-move not visited); BinaryConditionalOperator = F120. |
| **BSCBorrowChecker** DefUse (:173) + ActionExtract (:772) | → **RECURSE into children** | **NO** | none — a missing visitor still visits children, so borrow creations/uses inside an unhandled kind ARE seen. Borrow FNs come from HANDLER bugs (F119 cast breaks loan, F11 comma), NOT coverage gaps. |

**CONCLUSION: dispatch-coverage surface is SATURATED.** The two Nop-default dispatches (BSCIRBuilder,
BSCOwnership) have their reachable in-scope gaps filed (F120, F111); the borrow checker's recursing-default
makes it categorically gap-immune. New dispatch-gap FNs require a NEW expr/stmt kind reachable in valid _Safe
BSC that hits a Nop-default — and the C/BSC AST surface is fully enumerated above. Probed designated-init
(handled via InitListExpr), sizeof/alignof (no-op, unevaluated — correct). Do NOT re-run the dispatch oracle
without a genuinely new in-scope AST kind.

## Value-lowering Visit* dispatch (BSCIRBuilder.cpp:728-1313) — expr coverage (C2/C3)
- **Invariant**: every expression that produces or moves an `_Owned`/`_Borrow`/nullable value must be lowered to an Operand the analyses can track; unhandled exprs hit `VisitStmt` (1313) = Nop + void-constant (tracking LOST).
- **Coverage**: DeclRef, Integer/Floating/Character/String literals, Binary (incl. BO_LAnd/BO_LOr short-circuit :788), Unary, Call, Cast, ImplicitCast, DeclStmt, Return, Member, ArraySubscript, VAArg, Predefined, CompoundLiteral, Atomic, ImplicitValueInit, InitList, sizeof/UnaryExprOrTypeTrait, Paren, CompoundAssign, Conditional (full `a?b:c`), SafeExpr, GNUNull, CXXNullPtrLiteral. **SATURATED for in-scope tracking-relevant exprs.**
- **Hole**: `BinaryConditionalOperator` (`a ?: b`, GNU omitted-middle) NOT handled → VisitStmt Nop fallback → **F120 (filed)**. Other unhandled kinds (OffsetOfExpr, ChooseExpr/_Generic/StmtExpr/AddrLabelExpr) are OOS (GNU) or non-tracking (offsetof=constant). VisitStmt Nop fallback is SAFE only because no in-scope tracking-relevant expr falls through except F120.

## prescanLabels / scope-depth bookkeeping for goto cleanup (BSCIRBuilder.cpp:675-701, 1327-1366) — UNPROBED, NEW surface (2026-06-30)
**Invariant**: for a `goto L`, the set of owned-local scopes Drop'd via `emitScopeCleanup(TargetDepth)` must be EXACTLY the scopes that the goto edge actually exits — TargetDepth (the label's ScopeStack depth) must equal the runtime `ScopeStack.size()` at the label site. `prescanLabels` precomputes this for FORWARD gotos (label not yet lowered); `lowerLabelStmt` overwrites it with the runtime `ScopeStack.size()` once the label IS lowered.
**Peers**: lowerGotoStmt (uses LabelScopeDepth), lowerLabelStmt (writes runtime depth), prescanLabels (writes prescan depth), emitScopeCleanup, lowerSwitchStmt (pushes exactly 1 ScopeStack scope for whole body, NOT per-region).
**Depth-accounting in prescanLabels vs actual lowering:**
- CompoundStmt: prescan `Depth+1` ; lowering `lowerCompoundStmt` pushes 1 scope. MATCH.
- ForStmt w/ DeclStmt-init: prescan `+1` ; lowering pushes 1 (PushedInitScope). MATCH. **But the ForStmt BODY is itself a CompoundStmt → prescan goes +1 for ForStmt-init AND +1 for the body CompoundStmt; lowering: for-init scope (1) + body CompoundStmt scope (1). MATCH.**
- SwitchStmt: prescan `prescanLabels(SS->getBody(), Depth+1)`. The body is a CompoundStmt → CompoundStmt case ADDS ANOTHER +1 → a stmt directly under `switch{...}` is prescanned at `Depth+2`. **Lowering `lowerSwitchStmt` pushes exactly ONE ScopeStack scope (:620) for the whole body; case-region bodies are lowered as bare stmts (NOT re-entering lowerCompoundStmt). So a label `switch(x){case 1: L: ...}` has runtime ScopeStack depth `Depth+1` but PRESCAN depth `Depth+2`. MISMATCH (off-by-one, prescan too deep).**
**Candidates:**
1. (switch-case forward-goto label depth off-by-one) **`goto L` (forward) into a `case` body label, where an owned local lives in an OUTER scope at depth between switch-runtime and prescan.** prescan TargetDepth=Depth+2 > runtime label depth Depth+1 → emitScopeCleanup(Depth+2) drops FEWER scopes (cleans only scopes ABOVE Depth+2) than the goto edge actually exits → an owned local in the scope at Depth+1..Depth+2 is NOT Drop'd → **LEAK FN**, OR if it over-drops a different path → double-free. Needs forward goto so prescan (not lowerLabel overwrite) is authoritative. RANK 1 (clear arithmetic divergence, runtime-judgeable by valgrind).
2. (goto-into-switch) C may forbid `goto` jumping INTO a switch from outside, but a goto from one case to a label in the SAME switch body is legal; depth mismatch applies. Reachability check needed.
3. (LabelScopeDepth overwrite races prescan) if a label is lowered (runtime depth written) and a LATER forward goto reuses it — but a goto AFTER the label is a BACKWARD goto (label already lowered) so runtime value is correct. Only forward gotos use prescan. RANK 3.

### RESULT (2026-06-30): prescan switch-label off-by-one PROBED-SOUND (no observable FP/FN)
3 probes (forward goto out of owned block to switch-case label; free-on-both-paths double-free check; owned local in case region goto-to-later-case): ALL correctly diagnosed the leak at the goto (when un-freed) or ran clean (1 alloc/1 free, valgrind no errors) when freed. The prescan depth quirk does NOT produce an observable FP or FN because the ownership analysis independently catches an un-dropped owned local as a leak regardless of how many redundant Drop terminators the cleanup emits; a Drop on an already-moved value is a no-op. The mismatch only affects WHERE redundant no-op Drops land, never the leak/move verdict. SOUND.

## VisitArraySubscriptExpr missing shouldMove() (BSCIRBuilder.cpp:1141-1144) — UNPROBED, peer-asymmetry vs VisitMemberExpr/VisitDeclRefExpr
**Invariant**: an lvalue expr that names an _Owned/move-semantic value and appears in a move context (call arg, return, init of another owned) must lower to `Operand::createMove(P)` so the ownership analysis marks the place Moved; otherwise a later use of the same place is a use-after-move the analysis won't catch.
**Peers**: VisitDeclRefExpr (:732 calls shouldMove → Move), VisitMemberExpr (:1134 calls shouldMove → Move), VisitInitListExpr (:1209 promotes Copy→Move on shouldMove). **VisitArraySubscriptExpr (:1141) ALWAYS returns createCopy — NO shouldMove call.** This is the lone lvalue visitor that never produces a Move.
**Reachability question**: `int *_Owned arr[N]` is SHAPE-REJECTED (type-level). The only owned-array form is `int *_Owned _ArrayElem arr` (take_array_from_raw). Is `consume(arr[i])` (subscript move of an owned _ArrayElem) accepted/reachable? If yes, the missing Move → array element stays Owned after the move → (a) double-move/UAF FN on a 2nd consume, or (b) the whole array stays Owned → wrong leak accounting.
**Candidates:**
1. (subscript move-out FN) `consume(arr[i])` on owned `_ArrayElem` → Copy not Move → element not marked moved → 2nd `consume(arr[i])` or use-after-move accepted → runtime double-free. RANK 1 (clean peer asymmetry; reachability TBD).
2. (whole-array leak miscount) if subscript-Copy leaves the array Owned, a leak that should be cleared by the move stays flagged (FP) — over-strict but in-scope.
3. (shape-rejected) if subscript-move of owned _ArrayElem is forbidden by Sema, the gap is latent-unreachable like F39's TransferFunctions arm.

### RESULT (2026-06-30): PROBED-SHAPE-REJECTED (latent-unreachable)
For `arr[i]` to be a move, the array's ELEMENTS must be owned/move-semantic. `int *_Owned arr[N]` → "type of array cannot be qualified by '_Owned'" (rejected). Array-of-struct-with-owned-field → also rejected. The `_ArrayElem` owned form (`int *_Owned _ArrayElem arr`) is an owned-pointer-to-array; `arr[i]` yields a plain `int` (the pointee), NOT a move. So NO in-scope `arr[i]` is move-semantic → VisitArraySubscriptExpr's missing shouldMove is benign, exactly like F39's TransferFunctions::VisitArraySubscriptExpr arm. Same disposition: latent-unreachable, not filable.
