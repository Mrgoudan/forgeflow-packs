# BSCBorrowChecker.cpp

Source: `clang/lib/Analysis/BSC/BSCBorrowChecker.cpp`.

## DefUse::VisitArraySubscriptExpr / VisitReturnStmt / VisitUnaryOperator (BSCBorrowChecker.cpp:159-199) — read 2026-06-17, PROBED-SOUND

**Invariant**: DefUse classifies each operand Def/Use to feed liveness/loan tracking. ReturnStmt →
operand Use (:160). UnaryDeref → Use. Unary inc/dec → Def then Use (RMW). ArraySubscript (:189) → in
Use/Def context, marks BOTH base AND index as Use ("subscript reads its base pointer even on the
assignment LHS `p[i] = …`", :191) — CONTRAST the ownership AST VisitArraySubscriptExpr which skips the
index (sound only via CFG linearization); DefUse visits both directly.

**Peers**: LoansInScope (consumes DefUse); ActionExtract; the region/liveness solver.

**Probed SOUND**: subscript write+read through `_Borrow _ArrayElem` tracks base as use (no false
conflict / no missed use); element-aliasing conflict = F39 (FIXED in rebuild).

## Borrow-escape / return-lifetime (syntactic gate + region analysis) — read+probed 2026-06-17, PROBED-SOUND

**Invariant**: a returned `_Borrow` must not outlive its referent. TWO layers:
(1) SYNTACTIC GATE — a `_Borrow` return type requires a `_Borrow` PARAMETER ("no _Borrow
qualified type found in the function parameters, the return type is not allowed to be
_Borrow qualified"); conservative (even rejects returning a borrow of a GLOBAL, be3).
(2) REGION ANALYSIS — when the gate passes (borrow param present), returning a borrow of a
LOCAL is caught with "`x` does not live long enough", path-sensitively (incl. ternary arms).

**Probed SOUND**: be1/be2 (no param, &local) gate-rejected; be3 (global, no param) gate-rejected
(conservative FP, sound); be5 `return &_Mut x` + be6 local-borrow-var + be8 `c?p:&_Mut x` (param
present) → region "does not live long enough"; be4/be7 (return param referent) clean. No
dangling-reference FN.

## LoansInScope::SimulateBlock / Compute (BSCBorrowChecker.cpp:1229-1292) — read 2026-06-17, PROBED-SOUND

**Invariant**: a loan is in-scope at a point if it is in-scope on SOME path reaching
it (a "may" analysis). At a join, the loan set is the UNION of predecessors'
after-states (:1233-1236 `fact.clear()` then `SetFrom(fact, after[pred])` per pred;
SetFrom = monotone OR, confirmed by :1289 `changed |= SetFrom(...)`). Union is the
SOUND direction — over-approximating live loans never MISSES a conflict (an FN would
require under-approximation/intersection).

**Peers**: RegionCheck (LoansNotInScopeAt — region/NLL liveness that Kills loans,
:1250); the Gen/Kill at borrow-create (:1259) and overwrite (:1270); the worklist
fixpoint Compute (:1281, monotone+bounded → converges in any node order).

**Candidates**:
1. (C5 merge) union-at-join under-approximation FN → reasoned-unlikely (it's UNION,
   the conservative dir). Confluence probe: a mut-borrow live across an `if`, with a
   write to the referent inside the branch — the union must keep the loan in scope so
   the write conflicts. Probed below.
2. (reachability) entry block (no preds) → fact empty (no loans) — correct.
3. (ordering) Kill-out-of-region (:1250) before Gen-new (:1259) before Kill-overwrite
   (:1270) — ordering looks right; the region check is the real liveness oracle. SOUND by reading.

Two cooperating visitors:
- **`DefUse`** — liveness (Kill/Gen) for the borrow CFG worklist. Per Stmt, records which borrows are defined / used / killed.
- **`ActionExtract`** — extracts the *source paths* that a statement borrows from / freezes / consumes. Builds the `Sources` vector consumed by region/conflict analysis.

Plus `RegionCheck` (read separately).

## Conventions

`Action` enum has values like Use / Def / GetAddr / Assign. The visitor mutates a shared `Action` state to propagate semantics through child traversal.

`IsCastFromVoidPointer(E)` (:167) is a positive example: it walks `IgnoreParens`, `IgnoreImpCasts`, also conditional operator both arms. Used as the de-facto wrapper-strip standard for this file.

## Functions

### `DefUse::VisitBinaryOperator` — :92+
**Invariant**: for each binary op, determine which operand is Def vs Use, dispatch recursively.
**Branches handled**: `isAssignmentOp()` (LHS=Def, RHS=Use), then various Logical/Cmp. **No `BO_Comma` case.**
**Exemplar**: **F11** (filed) — `BO_Comma` falls through silently → use-tracking misses borrows in `(x, &_Mut y)`.
**Candidate (C2)**: re-audit for `BO_LAnd` / `BO_LOr` (short-circuit semantics may matter), `BO_PtrMemD/I` (rare in BSC but should be diagnosed if used).

### `DefUse::VisitMemberExpr` — :~144
**Invariant**: member access of a borrow-typed lvalue should be either Use or Def depending on outer context.
**Asymmetric peer**: `VisitUnaryOperator` for inc/dec on members re-visits the inner (double-visit). `VisitMemberExpr` keeps Action stationary; the base isn't visited explicitly.
**Candidate**: if a member of a borrow-pointer-typed field is accessed as an lvalue, does the base get recorded as a use? Possibly missed if not driven by an outer UnaryOperator/BinaryOperator.

### `IsCastFromVoidPointer` — :167
**Invariant**: returns true iff E (with all wrappers and conditional both-arms stripped) is a cast from a void pointer.
**Wrappers stripped**: `IgnoreParens`, `IgnoreImpCasts`, ConditionalOperator (BOTH arms via recursion).
**Use as gold standard**: any other "is this a Foo-shape" predicate in this file should match this thoroughness.

### `ActionExtract::VisitBinaryOperator` — :425+
**Invariant**: extract source paths for both operands of a binary op, with Action set per side.
**Same C2 hole as DefUse**: **No `BO_Comma` case** → F11.

### `ActionExtract::VisitBinAssign` — :449
**Invariant**: specialized handler for `=` assignment. If RHS is a `CompoundLiteralExpr` (per-field init), enter borrow-aware per-field source tracking; else fall back to generic Assign path.
**Wrapper check**: uses `IgnoreImpCasts()` — NOT `IgnoreParens()`. Probed (probe_paren_compoundlit_rhs): paren-wrapped `((struct S){...})` falls back gracefully — no exploitable bug *currently*, but the asymmetry remains a future risk if generic path stops covering some case.

### `ActionExtract::VisitArraySubscriptExpr` — :421
**Invariant**: visits the base of a subscript; index is **not visited**. Helper at :781 only recurses into `SubExpr` (the base).
**SHAPE-REJECTED 2026-05-19**: probed `arr[helper(&_Const x)]` (mut+const conflict in index) — diagnostic **fires correctly**. Reason: Prologue's `TransformCallExpr` / `TransformUnaryOperator` hoists CallExpr and prvalue UnaryOp into temp DeclStmts BEFORE ActionExtract runs. So by the time ActionExtract sees a subscript, the index is guaranteed side-effect-free (it's a temp DRE or a literal). The unvisited index is intentional, not a bug.
**Implication**: this is a load-bearing invariant — ActionExtract's per-expression non-recursion of children RELIES on Prologue hoisting. The real defect class to hunt: **expression kinds whose children Prologue does NOT hoist**. See C4 / candidate list below.

### `ActionExtract::VisitConditionalOperator` — (not present)
**Note**: confirmed absent in this file. Generic fall-through to `VisitStmt` (iterate children) adds source paths from each arm into the same `Sources` vector. Downstream consumer `Sources[0]` may lose one arm. Probed (probe_borrow_thru_ternary): works **because** the Sema Prologue rewrites `?:` into IfStmt before this analyzer runs — load-bearing prologue normalization.
**Candidate (C4)** — partially CONFIRMED 2026-05-19 as **F16** (IJOEJP): same protection does **not** apply to `_Generic` / `__builtin_choose_expr` — borrow checker flags conflicts in UNSELECTED arms. F09 was codegen-side; F16 is analyzer-side. `BinaryConditionalOperator` (GNU `?:`) still not probed.

## Candidate status (ranked, with progress)

1. **C3+C6/Subscript index** — **PROBED-SHAPE-REJECTED 2026-05-19** — Prologue's CallExpr / prvalue-UnaryOp hoisting makes side-effects in subscript index land in their own CFG element, processed correctly.
2. **C4/BinaryConditional GNU `?:`** — **FOLDED-F16 2026-05-19** — same root cause; GNU-only construct.
3. **C2 re-audit BO_LAnd/BO_LOr** — **UNPROBED** but C2 is CONFIRMED at this file via F11 (BO_Comma). Sibling variant; likely not separately filable. **Drop unless invariant differs.**

## Not yet read

- `RegionCheck` — partially READ (PopulateInference at :1568, EnsureBorrowSource at :1702, RelateRegions at :1728, PreprocessForParamAndReturn at :1741 read 2026-05-20).
- `ActionExtract::VisitCallExpr` — **READ** L469-478. Iterates `CE->arguments()` only; **`CE->getCallee()` is NOT visited**. If `Kind == Noop`, becomes Use; else if `Dest != nullptr`, becomes Init. Sets `op = RHS` for arg visiting.
  - **Candidate (C3 callee not visited)**: a call like `(s->fp)(arg)` where `s` is a `_Borrow` reads `s` via the callee MemberExpr — that read is invisible. **Mitigated**: Prologue's `TransformCallExpr` (SemaDeclBSC:786-803) only transforms args + hoists non-void CallExpr — callee subexpression NOT rewritten. So this hole is reachable in principle. **Likely low-impact**: `s` is a borrow; reads of a borrow don't violate ownership; the only conflict would be region-lifetime, and the borrow's loan generation likely happens at the variable's introduction not at use-site. UNPROBED — likely SHAPE-REJECTED because function-pointer calls through borrow are unusual in valid BSC.
- `ActionExtract::VisitMemberExpr` — **READ** L658-706. LHS: visits base, builds Dest path. RHS: visits base (with pathDepth++), builds Src path; if METype is borrow-qualified, `Member->setDecl(D)` so reborrow constraints attach. Arrow case prepends "*" path. At pathDepth==0, pushes Src into Sources. **Properly visits base** in both LHS and RHS — no obvious symmetric hole.
- `DefUse::VisitCallExpr` — **READ** L117-122. Same pattern: visits args only; **callee not visited**. Same C3 hole as ActionExtract version; same low-impact assessment.
- `DefUse::VisitMemberExpr` — **READ** L146-155. Asymmetric: `Action==Def && isAssign` → flips to Use, visits base. `Action==Def && !isAssign` (compound-assign first pass, ++/-- first pass): base NOT visited. **Reasoned-safe**: subsequent `Action=Use` second pass DOES visit base (see VisitBinaryOperator:101-105, VisitUnaryOperator:177-181). `Action==None` (top-level ExprStmt of bare `m.f;`): base NOT visited — but `m.f;` as a pure read-statement is rare and side-effect free; harmless.
- `markFieldInit / tryPromoteParent` — **READ** BSCIRInitAnalysis.cpp:525-606. Bottom-up: when all siblings at level N are Init, promote parent at level N-1. Union special case (NumSiblings==0, RD->isUnion()): single variant init → promote whole union → `clearUnionFieldEntries`. Array elements never promote array (NumSiblings==0 && !isUnion → returns). Consistent with documented array limitation. No defect candidate.

### `ActionExtract::VisitUnaryAddrConstDeref` vs `VisitUnaryAddrMutDeref` — :742-779
**Invariant**: both flavors model `&_Const *e` / `&_Mut *e` (re-borrow through deref) — set RNR/BK/Kind=Borrow, visit subexpr, then if `Sources[0]->ty` is a pointer add a `*` deref path.
**Asymmetry**: `VisitUnaryAddrConstDeref` has `if (Sources.empty()) { Kind = Action::Init; return; }` (:747). `VisitUnaryAddrMutDeref` does NOT — line 773 unconditionally indexes `Sources[0]`. If a sibling visitor leaves `Sources` empty (e.g., `VisitCStyleCastExpr` early-returns for CK_NullToPointer at :483-485), the mut variant would dereference an empty vector → undefined behavior / analyzer crash.
**Candidate (C1 Ignore-asymmetry)**: probe `&_Mut *X` where X is a sub-expression that the analyzer drops without pushing a Source. **UNPROBED**.
**Reachability**: Prologue hoists most complex subexprs into DREs that DO push Sources, so reaching the empty-Sources branch likely requires a `_Mut`-deref of a null-cast or similar dropped construct.
- `ActionExtract::VisitInitListExpr` — **READ** L592; uses `isa<InitListExpr, CompoundLiteralExpr>` at sibling `VisitDeclStmt:573` — the correct idiom that BSCOwnership lacks (see F17).
- `ActionExtract::VisitCStyleCastExpr` — **READ** L480-520; handles nested CSCE specifically for CK_NullToPointer; deref-add logic for borrow casts

### `Liveness::SimulateBlock` — :1022-1072
**Invariant**: backward dataflow per CFG block. For each CFGStmt walked in reverse, DefUse computes defs/uses; Kill defs (no longer live), Gen uses (now live).
**Peers**: DefUse (provides per-stmt defs/uses), LoansInScope (parallel forward dataflow).
**Candidate (C1/C3-folded)**: any DefUse coverage gap propagates here. F11 (BO_Comma missed in DefUse::VisitBinaryOperator) means comma-operand variables are not Gen'd as uses → liveness underestimates → region constraints under-applied → potential false negative on borrow conflicts. **FOLDED into F11**.
**Re-read 2026-05-20**: confirmed the flow:
1. Start with successors' live-in facts unioned (line 1027-1030).
2. Walk CFG elements in reverse (line 1033).
3. CFGStmt: build DefUse, Kill defs, Gen uses (line 1041-1057).
4. CFGLifetimeEnds: collected for callback but doesn't affect liveness (line 1062-1066, comment).
5. Callback invoked per element with point + S + fact + LifetimeEnds info.
**Implication**: any AST-kind handled by `DefUse::Visit*` correctly propagates here. F11 is the only known DefUse gap. The handling of CFGLifetimeEnds at line 1062 separately captures end-of-life info for the borrow inference (PopulateInference) but not for liveness. The Point indexing at line 1068 uses distance-from-end for reverse iteration, consistent with downstream consumers.

### `Liveness::Compute / Walk` — :1007 / :1080
**Invariant**: fixed-point iteration over CFG; Walk invokes callback per Statement with the live-on-entry fact + point + S + LifetimeEnds info.
**Standard backward dataflow**.

### `Environment::SuccessorPoints` — :863-894
**Invariant**: returns the set of points "immediately after" a given Point. If not at block-end, the next index in same block. If at block-end, all non-empty immediate successors (walking through empty-block chains).
**Empty-block traversal**: fixed-point loop accumulates successors-of-empty-successors until no new ones found.
**Candidates**:
1. Cycle of empty blocks — terminates correctly (size unchanged → exit). UNPROBED but reasoned-safe.
2. Out-of-range `point.blockID` (line 866 uses `*(cfg.nodes_begin() + point.blockID)`) — assumes blockID < block count. Defensive check might be missing if called with invalid Point. UNPROBED.

### `ActionExtract::BuildAction` — :229-295
**Invariant**: emits one of `ActionAssign / ActionBorrow / ActionInit / ActionStorageDead / ActionUse / ActionNoop` per CFG point, based on `Kind`.
**Switch coverage**: Assign, Borrow, Init, StorageDead, Use, default→Noop. Exhaustive.
**`Init` branch (line 250-269)**: if Dest is tracked → `GenerateImplicitAssign` + ActionInit. Else → ProcessDeref + ActionInit.
**`ProcessDeref`**: only creates DerefSources for `isBorrowQualified()` or `withBorrowFields()` types. Owned-only fields not included — appropriate, since owned ownership is not a borrow-lifetime concern.

### `RecursiveForFields` — :314-333
**Invariant**: walks record fields, accumulating borrow-deref paths into Res.
**Asymmetry observation**: checks `isBorrowQualified()` (line 321) and `withBorrowFields()` (line 327) — does NOT recurse into owned-pointer fields whose pointee is a struct. So borrow paths embedded INSIDE an owned struct field aren't tracked here. Probably fine in practice — owned ptr's pointee has its own lifetime.

### `Path::prefixes / supportingPrefixes` — header :174-225
**Invariant**: `prefixes()` returns all parent paths walking via `base`. `supportingPrefixes()` is the same but stops at `*r` where r is const-borrow (since `*r` is a copyable view, r doesn't need to remain valid).
**Used**: by `LoansKilledByWriteTo` (kill loans when writing to any prefix), `FindLoansThat*` (intersect/freeze tests).
**Candidate (no defect)**: const-vs-mut borrow distinction at supportingPrefixes is correct per Rust-like NLL semantics.

### `FindLoansThatIntersect` / `FindLoansThatFreeze` / `FrozenByBorrowOf` / `structurallyEquals` — BSCBorrowChecker.cpp:1501-1598, header :230-236 — UNPROBED (fresh binary 34e6f26e)
**Invariant**: a loan L conflicts with a new access to path P iff L's path and P share a prefix relationship: either some prefix-of-P equals L's path (L "contains" P — `s` borrowed blocks `s.f`), or some prefix-of-L's-path equals P (L "is contained in" P — `s.f` borrowed blocks `s` access). `FrozenByBorrowOf` walks the loan path DOWN to its Var root, returning every prefix along the way (the set of paths whose mutation would invalidate the loan). `supportingPrefixes` of the loan path is the symmetric "P reaches into L" direction, stopping at const-borrow derefs (copyable view). `structurallyEquals` compares `(type, fieldName)` recursively via `base` — **it does NOT compare `ty`/`UsesArraySubscriptNotation`/`D`**, only the field-name spine.
**Peers**: `CheckMove` (Deep/Write via Intersect), `CheckRead` (Deep/Read via Intersect), `CheckMutBorrow` (Deep/Write), `CheckShallowWrite`/`CheckStorageDead` (Shallow/Write via Freeze).
**Candidates**:
1. SIBLING-DISJOINT OVER-COLLAPSE — borrow `s.f` `_Mut`, then access disjoint sibling `s.g`. By the prefix model, `s.g`'s prefixes are `{s.g, s}` and the loan path is `s.f` (prefixes `{s.f, s}`). Intersect test: is any prefix of `s.g` == loan path `s.f`? `s.g`≠`s.f`, `s`≠`s.f` → NO. Is any supporting-prefix of loan `s.f` == `s.g`? supporting-prefixes of `s.f` = `{s.f, s}`; `s`≠`s.g`, `s.f`≠`s.g` → NO. So sibling access should be ALLOWED. Probe: confirm `s.f` borrowed does NOT block `s.g` move/read (FP direction if it does). REACHABILITY: trivially reachable (struct with 2 owned fields).
2. SAME-FIELD `_Const` vs `_Mut` re-borrow — `b1 = &_Const s.f` then `b2 = &_Mut s.f` while b1 live. The `_Mut` borrow calls `CheckMutBorrow`→Deep/Write→Intersect; loan `s.f` (Shared) prefixes `{s.f,s}`; new path `s.f` prefixes `{s.f,s}`; match on `s.f`→`ForMutWhenImmut` should fire (a shared loan freezes a mut borrow of the SAME field). Baseline: `_Mut` then `_Const` → `ForImmutWhenMut`? Probe: confirm both directions reject. SYMMETRY lens.
3. WHOLE-STRUCT `_Mut` of `s` while field `s.f` pre-borrowed `_Const` — `b1=&_Const s.f` then `b2=&_Mut s` (whole struct). New path `s` prefixes `{s}`; loan `s.f` supporting-prefixes `{s.f, s}`; `s` matches → `ForMutWhenImmut` fires (correctly rejects: whole-struct mut-borrow invalidates the field loan). Baseline: `_Const s.f` then `_Const s.g` should be ALLOWED (shared loans coexist). Probe asymmetry. COMPOSITION lens.
4. `structurallyEquals` ignores `UsesArraySubscriptNotation` — `arr[0]` (subscript, notation=true) and `*arr` (deref, notation=false) of the same base compare EQUAL (same fieldName `*`, same base). Already noted in _probed as documented "unified element" intent — but worth ONE differential on a struct-FIELD that is an array vs a sibling scalar to confirm the collapse is field-name-only, not type-driven. Lower priority.

### `InferenceContext::AddVar / AddOutLives / CapVar` — header :565-585
**Invariant**: AddVar appends a new region variable. AddOutLives adds a `Sub : Sup @ Point` constraint. CapVar marks a region variable as "frozen" (cannot grow during Solve). Standard NLL inference helpers.

### `getFieldType` — BSCIRInitAnalysis.cpp:466-483
**Invariant**: walks Path indices through a record, returning the leaf type. Asserts mid-path types are records.
**Assertion** at line 472: `assert(RD && ...)`. If Path leads through a non-record, crash. Defensive against caller bugs; caller (per FP.Indices construction) always builds valid field chains.

### `ActionExtract::VisitMemberExpr` — :658-706 — READ 2026-05-20
**Invariant**: for LHS, builds `Dest` path by walking base then appending member name; for RHS, builds `Src` path same way + pushes to `Sources` at outermost level. Arrow access (`->`) adds an implicit `*` path component.
**Base IS visited**: contrasts with the earlier note's question on DefUse::VisitMemberExpr; both DefUse (line 154 if Action==Use) and ActionExtract (line 663/678) visit the base.
**Offset-of guard** (lines 685-688): `if (!Src) { --pathDepth; return; }` — intentional bypass for `((T*)0)->field` offset-of pattern.
**No defect found**.

### `ActionExtract::VisitDeclRefExpr` — :522-558 — READ 2026-05-20
**Invariant**: produces Path with just the var name (LHS sets Dest; RHS sets Src + pushes to Sources at outermost). Borrow-qualified DREs and tracked-type DREs get `setDecl(D)` and `D = DRE->getDecl()`.
**Arrow handling**: if isArrow flag is set (from outer ME), adds `*` path component.
**No defect found**.

### `ActionExtract::VisitDeclStmt` — :560-590 — READ 2026-05-20
**Invariant**: for a single VarDecl, sets Dest to the var. If init is InitListExpr/CompoundLiteralExpr on a struct/array of tracked type, sets BuildOnGet=false and visits init (per-field path tracking). Otherwise Kind=Assign, op=RHS, visits init.
**Edge handling**: `Kind = Action::Init` if RNL/RNR invalid or Sources empty (e.g. `int *borrow p = (int *borrow)NULL`).
**No defect found**.

### `ActionExtract::VisitInitListExpr` — :592-656 — READ 2026-05-20
**Invariant**: per-field/per-element iteration:
- struct/union with tracked type: loops over `RT->getDecl()->fields()`, calls `Visit(ILE->getInit(Index))` per field.
- array with tracked element: loops over inits with synthetic `[N]` path.
- otherwise: visits each init without per-element path.
**Union concern**: loop at :602 iterates ALL fields of a union, but Sema forbids `_Owned`/`_Borrow` qualified union fields → **SHAPE-REJECTED**. Union path is unreachable from valid BSC source.

## DefUse::VisitArraySubscriptExpr — MISSING (root of F39)

**Invariant (intended)**: For `a[i] = X` (LHS of BinAssign), `a` should be recorded as USE (the pointer is read to compute the address), `i` should be recorded as USE (the index is read). The base `a` should NOT be a DEF because writing through `a[i]` doesn't write to `a` itself, only to `*(a+i)`.

**Reality**: No `VisitArraySubscriptExpr` defined. ArraySubscriptExpr falls through to `VisitStmt` (line 164) which iterates children with the inherited Action. When the parent context is `BinAssign` LHS (Action=Def, isAssign=true), the base `a` is visited with Action=Def → pushed into `defs`. `Liveness::SimulateBlock` (line 1048) then `Kill()`s `a` at this statement.

**Peer**: `VisitMemberExpr` (lines 146-155) handles the analogous `s.f = X` case by switching Def→Use when isAssign. ArraySubscriptExpr lacks the analogous switch — peer asymmetry.

**Consequence (PROBED-confirmed-F39)**: `int *_Borrow _ArrayElem a = &_Mut arr[0]; a[1] = X; int *_Borrow b = &_Mut arr[1];` compiles clean even though `a` and `b` mutably alias `arr[1]`. Without the intermediate `a[1] = X`, correctly rejected. Filed as IJONYD.

## Cycle 12: CheckMove/CheckMutBorrow/CheckRead/CheckShallowWrite/CheckStorageDead, FindLoansThatFreeze, Path::to_string

### CheckMove/CheckRead/etc. (BSCBorrowChecker.cpp:1466-1500)

**Invariants**: thin dispatchers to `CheckBorrows(Depth, Mode, path)` or `FindLoansThatFreeze`. Move uses Deep/Write semantics (intersects loans), Read uses Deep/Read, MutBorrow uses Deep/Write, ShallowWrite uses Shallow/Write, StorageDead uses freeze.

### FindLoansThatFreeze (line 1464-1490)

**Invariant**: a loan freezes a path if (a) writing to the borrowed-from prefix would invalidate the loan, OR (b) writing to a sub-path within the borrowed region would. Uses STRING comparison via `Path::to_string()`.

**Reachability**: Path::to_string normalization gap could conflate distinct paths. Inspection of Path::to_string (BSCBorrowChecker.h:223-235) shows:
- Var → fieldName
- isDeref ("*") → "*" + base
- field after deref → "(*x).f" form
- array → base + "[]"
- field after non-deref → base + "." + fieldName

So `a[0]` and `a[1]` both stringify as `*a` (subscript-as-deref); same path identity. By-design (arrays have unified-element borrowing per `_Borrow _ArrayElem` semantics).

`*p` and `p[0]` produce same path → consistent.

### Cycle 12 conclusion

Path-based borrow-conflict detection uses unified path identity for array elements. No probe-worthy gap; the unified handling is intentional for `_ArrayElem` design.

## 2026-05-21 Explorer #N RegionInference internals — DEEP READ

### `InferenceContext::Solve` (BSCBorrowChecker.cpp:917-951) — UNPROBED frontier

**Invariant**: Iterate `for (Constraint : constraints)` repeatedly. For each constraint `(sub, sup, point)`, run `dfs.Copy(Sup, SubDef.value, point)` to propagate from Sup's region into Sub's region all points reachable from `point` (visiting only points already in Sup). The outer-`while(changed)` ensures fixpoint. Capped variables MUST NOT grow during Solve — if they do, `llvm_unreachable` fires (UB in release).

**Peers**:
- `AddLivePoint` (line 900-910) — also subject to capped invariant
- `DFS::Copy` (line 954-997) — the propagation primitive
- `Environment::SuccessorPoints` (line 863-894) — CFG successor lookup used by DFS
- `PreprocessForParamAndReturn` (line 1741-1774) — pre-populates and caps the Free region

**Candidates**:
1. **Free region cap violation via constraint propagation**: PreprocessForParamAndReturn pre-populates FreeRV with `(blockID, 1..size)` for every block PLUS EndPoint, then caps it. If `RelateRegions` ever generates a constraint with FreeRV as the **sub** at a point that's already in FreeRV — fine (AddPoint returns false). But if dfs.Copy from Sup expands and reaches a point that's somehow NOT in the pre-populated set, llvm_unreachable fires → compiler crash / UB. The pre-population covers all 1..size points; SuccessorPoints only emits points in [1, size] range. So the surface seems closed BUT it depends on `_Borrow`-qualified parameter scenarios feeding constraints to FreeRV. UNPROBED.
2. **Empty-CFG-block dataflow corner**: If a CFG block has zero elements but is in `cfg.const_nodes()`, the pre-population adds zero points for it. But `SuccessorPoints` walking through it (line 877-886 empty-block recursion) won't emit a point for it either. Consistent. UNPROBED but reasoned-safe.
3. **DFS termination correctness**: DFS::Copy gates extension on `From.MayContain(p)`. If Sup's region has a "hole" (P1, P3 but not P2 on the CFG path P1→P2→P3), DFS stops at P2 and never adds P3 to To. This is correct by NLL semantics (P3 is unreachable from P1 within Sup) but could potentially under-approximate Sub's region if Sup's holes are themselves caused by Solve race conditions. UNPROBED.
4. **Outer-while fixed-point race when constraints reference each other**: Constraints `A→B@P1` and `C→A@P2`. Iteration order matters; the outer-while iterates until quiescence. SAFE if Region::AddPoint is monotone (only grows). UNPROBED but reasoned-safe.
5. **AddLivePoint duplicate insertion** (line 905): `if (definition.value.AddPoint(P))` — only enters the inner cap-check branch when P is new. If P is duplicate, no growth, no check. Correct.
6. **Empty-CFG function body** with tracked return type — pre-population adds ZERO interior points, only EndPoint. Capped. If any non-trivial constraint references FreeRV... but such constraints arise only from `&_Mut`/`&_Const` actions which require body. Surface unreachable from valid input.
7. **The DFS pops a point and finds From doesn't contain it (line 969)** — continues without visiting successors. But the point's CFG-successors may all be in From! Example: From = {P1, P3}, P2 not in From, CFG: P1→P2→P3. DFS from P1 finds P1 in From, pushes [P2]. Pops P2, not in From, skips. P3 never reached, despite being in From and CFG-reachable via P2. This is a TRUE under-approximation. **PROBABLY INTENTIONAL** — it represents the semantic notion "the region's lifetime hit a hole at P2, so P3 even if back-in is a separate region instance." But: is there any constraint generation that should cause this scenario to under-approximate Sub? UNPROBED — top candidate.

### `DFS::Copy` (BSCBorrowChecker.cpp:955-998)

**Invariant**: starting from `StartPoint`, walk forward via SuccessorPoints, adding to `To` every point that is in `From` and reachable from StartPoint (where "reachable" is via points that are themselves in `From`). Returns true if any addition happened.

**Trap door**: line 985-993. `SuccessorPoints.empty()` triggers the EndPoint extension. **But this triggers on any leaf in the CFG, not just function-exit leaves.** If a CFG block has no successors (e.g., unreachable end, no-return call), the EndPoint gets added to `To` if it's in `From`. Is this the right semantic? — see candidate below.

**Candidates**:
1. **`SuccessorPoints.empty()` → EndPoint extension at any leaf**: If the CFG has a no-return path (e.g., `abort()` or `__builtin_unreachable()` end), the DFS reaches that leaf, finds empty successors, and extends `To` with `EndPoint` if it's in `From`. The semantic claim is "this region extends to function end via this leaf." But a no-return leaf is precisely the case where the function does NOT reach EndPoint. So extending `To` with EndPoint here is **over-approximation**. UNPROBED — could allow a borrow to be reported as outliving the function via an abort-only path.
2. **The empty-successor leaf detection conflates EXIT block successors with `noreturn`-call successors**. The EXIT block typically has no successors in CFG terms, but it's reached by every normal return. A `noreturn` call has no successors but is NOT a normal return. Both are treated identically by SuccessorPoints. UNPROBED. Could yield false positives / negatives.

### `Environment::SuccessorPoints` (BSCBorrowChecker.cpp:864-895)

**Invariant**: for a point not at end of its block, return next index in same block. For a point at end of block, return the index-1 point in each non-empty immediate successor, transitively walking through empty blocks.

**Candidate**: 
1. **`*(cfg.nodes_begin() + point.blockID)` (line 866)** assumes blockID < block count AND blockID indexes correctly into the Blocks vector. Blocks vector stores in insertion order; BlockID assigned in insertion order via NumBlockIDs++. So position-N block has BlockID-N. Safe BUT only if no block was created and then removed. Verified safe assumption (CFG doesn't remove blocks).
2. **Successor block at index 1 (line 889)**: succ has BlockID 1 = index 1, which is the first element. Matches the pre-population. Safe.

### Probe plan

The freshest hypothesis is **candidate #1 in DFS::Copy** — does a `noreturn` leaf cause over-approximation of region extending to EndPoint?

Probe shape: borrow `&_Mut x` where x is a local; hold the borrow live across a `noreturn`-only branch (e.g., `if (cond) abort(); use(b);`). If the branch ABORTS and the OTHER branch doesn't extend the borrow's region to EndPoint, then the borrow is correctly contained. If the analyzer over-extends via the abort leaf, may produce spurious "x does not live long enough" diagnostic.

Alternative probe: pass borrow back to caller from a branch that calls abort vs a branch that returns normally. The free-region constraint should be tight.

Re-audited remaining un-noted ActionExtract methods: `VisitReturnStmt` (708), `VisitUnaryAddrConst/Mut` (735/759), `VisitArraySubscriptExprOrUnaryDeref` (781), `VisitIncrementDecrementOp` (353), `VisitInitListExpr` (struct/array/union branches). Each invariant checked vs Prologue's hoisting protection. Findings:

- `VisitIncrementDecrementOp` produces self-`ActionAssign(p ← p)` via GenerateImplicitAssign — over-conservative (false-positive surface) for `_Borrow _ArrayElem` but not exploitable because the self-region constraint is trivially satisfied.
- `VisitInitListExpr` anonymous-nested-struct with borrow field — correctly tracked (probe_explorer_7).
- `VisitInitListExpr` array partial-init of struct-with-borrow — Sema rejects (`type contains nonnull pointer must be properly initialized`), shape-rejected.
- `VisitCStyleCastExpr` early-return on inner-CK_NullToPointer (line 481-486) — only fires when inner is CStyleCastExpr, not ImplicitCastExpr; `(int *_Borrow)nullptr` reaches the visit normally, no Sources pushed because nullptr has no Decl. Reassigning a borrow var to nullptr correctly kills its prior loan (probe_explorer_2: clean second mut-borrow after nullptr reassign).
- PreprocessForParamAndReturn's one-region-for-all-params model — F38 already covers the conservative-rejection surface; indirect-call return path through `ident` correctly diagnoses `y does not live long enough` (probe_explorer_5).
- DefUse coverage of compound-assign LHS-ArraySubscript (`a[i] += X`) — adds `a` to BOTH defs and uses (compound-assign visits LHS twice in VisitBinaryOperator), so Kill+Gen restores liveness. Distinct from F39 simple-assign which is uses-empty.

Conclusion: BSCBorrowChecker.cpp surface in scope is **saturated** for current campaign. Remaining un-probed candidates (compound-literal-paren wrap of BinAssign RHS at line 449, VisitUnaryAddrMutDeref empty-Sources guard) are either DEFENSIVE-CODE-INCONSISTENCY (unreachable from valid BSC) or duplicate fold of existing F-numbers.

## Assertion-precondition audit (2026-05-21 explorer cycle)

Goal: probe `llvm_unreachable` and `assert` sites in BSC analyzer code for input shapes that defeat the precondition. **Critical finding:** the clang build is Release with `LLVM_ENABLE_ASSERTIONS=OFF` (CMakeCache.txt:CMAKE_BUILD_TYPE=Release, LLVM_ENABLE_ASSERTIONS=OFF). This means:
- `assert(...)` is dead — does not crash but does not protect either. Violations silently propagate.
- `llvm_unreachable(...)` becomes `__builtin_unreachable()` in Release without assertions — UB on reach.

Audit by site:

- **BSCBorrowChecker.cpp:232/244** `assert(!RNL.isInvalid() && !RNR.isInvalid())` in BuildAction(Assign/Borrow). All callers (VisitBinAssign:463, VisitDeclStmt:583, VisitInitListExpr:614, VisitInitListExpr-array:645, VisitReturnStmt:724) defensively switch `Kind = Action::Init` when RNL/RNR invalid BEFORE BuildAction runs. Even bare borrow stmt `(void)(&_Mut x)` — though VisitUnaryAddrMut sets `Kind=Borrow` and RNR but RNL stays invalid — produces no observable misbehavior (probes 1, 6, 7b: subsequent borrow conflicts still detected correctly). Conclusion: **paired with caller guards; no exploitable path**.

- **BSCBorrowChecker.cpp:341/343** `assert(Source->D != nullptr)` / `assert(!SourceRN.isInvalid())` in GenerateImplicitAssign. Both inside `if (IsTrackedType(Source->ty) && Source->D != nullptr)` — redundant defensive asserts.

- **BSCBorrowChecker.cpp:1692** `assert(regionMap.find(RN) != regionMap.end())` in getRegion. Callers: LoansInScope at :1121 (AB->RNR) and :1125 (AA->DerefRN). Both region names are registered by `PopulateInference` BEFORE `LoansInScope` runs: AB->RNR via RelateRegions(:1608)→getRegionVariable; AA->DerefRN via explicit getRegionVariable(:1616) + RelateRegions(:1618-1619). No path reaches LoansInScope without prior registration.

- **BSCOwnership.cpp:482/496/511/561/574/589** `llvm_unreachable("Unexpected branch")` in initOPS/initS. The `Source` enum has 3 values (OPS, S, BOP), and the unreachable fires only for source ∉ {OPS, S}. Callers: initOPS only invoked with Source::OPS (line 441) and recursive (preserves OPS); initS only invoked with Source::S (line 453), recursive (S), or from initOPS (with OPS — handled by S-branch). initBOP handles all 3 sources separately. **Defensive enum exhaustion, unreachable from valid input.**

- **BSCIRInitAnalysis.cpp:472** `assert(RD && "expected record type along path")` in getFieldType. Walks `It->getType()` per Field index. Caller (getFieldPath) terminates Indices push when CurTy is non-record. So FP.Indices length ≤ depth of nested-struct chain. Each step in getFieldType uses the I-th field of the prior RD; if I-th field is a non-record/pointer, the walk exits the inner for-loop (line 478) but Ty was already set to It->getType() on the last assignment. Next outer iteration's `Ty->getAsRecordDecl()` returns null → assert fires. **However**, this requires FP.Indices to have more entries than the actual nested-record depth, which getFieldPath prevents by its own non-record termination. Paired.

- **SemaBSCSafeZone.cpp:1183** `assert(!CurFunction->InsCompoundSafeZone.empty())` in PopInsSafeZone. Paired with InsSafeZoneRAII (Sema.h:5021-5027). `setInstantiationSafeZoneSpecifier` (1192) does Pop+Push only inside `size() != 0` guard.

- **SemaDeclBSC.cpp:34/60** — paired with caller-side `isConstexprSpecified()` check.

- **SemaBSCOverload.cpp:32** — operator overloading (uses traits / member fns), **OUT OF SCOPE**.

- **SemaBSCDestructor.cpp / SemaBSCOwnedStruct.cpp / SemaBSCCoroutine.cpp / SemaBSCTrait.cpp / SemaTemplateInstantiateDeclBSC.cpp** — all OUT OF SCOPE per OUT_OF_SCOPE_KEYWORDS.txt (owned struct member methods, traits, coroutines).

- **BSCBorrowChecker.cpp:907/936** — `llvm_unreachable("Free region should not grow anymore!")` in RegionInference. Per steering hint: skipped, prior explorer #7 could not reach.

- **RewriteBSC.cpp:690** `llvm_unreachable("Unreachable branch")`. Fires only if a Decl in DeclsWithoutBSCFeature is not a TagDecl. Analysis: DeclsWithoutBSCFeature filtered by FindDeclsWithoutBSCFeature:515-527 to {Enum, Function, Record, Var}. DeclList populated by Steps 1-3 with {RecordDecl, EnumDecl, TypedefDecl/TypeAliasDecl}. Intersection is {Enum, Record} — both TagDecl. Function and Var never reach DeclList in the loop at :651. **Defensive; unreachable from valid input.**

- **BSCNullabilityCheck.h:125** `llvm_unreachable("Unknown error type")` in flushDiagnostics default arm. Enum NullabilityCheckDiagKind has 7 values + Max; switch covers all 7 (lines 113-122). Defensive enum exhaustion.

**Conclusion (2026-05-21):** The BSC analyzer's `llvm_unreachable` and `assert` sites are uniformly paired with caller-side guards or are defensive enum-exhaustion. No exploitable assertion-precondition attack surface remains within scope. The technique is saturated for this codebase.

## DefUse::{VisitDeclStmt, VisitBinAssign, VisitMemberExpr, VisitUnaryOperator, VisitUnaryDeref} — READ 2026-05-22

**Coverage matrix** (DefUse-only):
- ✅ VisitDeclStmt: push VarDecl to defs; if has init, Action=Use and visit init.
- ✅ VisitBinAssign: Action=Def for LHS (via SaveAndRestore isAssign), then Action=Use for RHS.
- ✅ VisitCallExpr: Action=Use for arguments (callee NOT visited — F24 area).
- ✅ VisitDeclRefExpr: push to defs/uses based on Action.
- ✅ VisitMemberExpr: if Action=Def && isAssign, flip to Use (handles `s.f = ...` correctly).
- ✅ VisitUnaryDeref: Action=Use for subexpr (handles `*b = ...` correctly).
- ✅ VisitUnaryOperator (generic): incdec → visit twice (Def + Use); else just subexpr.
- ❌ VisitArraySubscriptExpr: **MISSING** (F39 — falls to VisitStmt → children iter without Def→Use flip).
- ❌ VisitCStyleCastExpr: not present; falls to VisitStmt which iterates children. Reasoned-safe (cast is identity for var-tracking).

Top remaining UNPROBED: only F39's surface (already filed). Other shapes folded or correctly handled.

## ActionExtract::VisitUnaryAddrMutDeref vs VisitUnaryAddrConstDeref (BSCBorrowChecker.cpp:743-779) — C4 asymmetry, 2026-05-29
**Invariant**: extracting the borrow Source for `&_Mut *X` / `&_Const *X` must handle
the case where Visit(X) yields NO Sources.
**ASYMMETRY (candidate, potential compiler crash)**: VisitUnaryAddrConstDeref (:743) checks
`if (Sources.empty()) { Kind=Init; return; }` before `Sources[0]`. VisitUnaryAddrMutDeref
(:767) does NOT — accesses `Sources[0]->ty` directly. If a `&_Mut *X` leaves Sources empty,
that's OOB on an empty SmallVector → crash/UB. The const path's guard proves empty is reachable.
**Probe outcome (2026-05-29): SHAPE-REJECTED (unreachable, not filed).** The empty-Sources case arises only for `&_Const *X` where X is an immutable non-place (e.g. string literal: `&_Const *"abc"` compiles; reaches ActionExtract with empty Sources → Init guard). The `&_Mut` counterpart is rejected by a SEMA GATE before ActionExtract (`&_Mut *"abc"` → "cannot take mutable borrow through string literal; string literals are immutable"). For mutably-borrowable X (places, or call/binop results hoisted to tracked temps by the Prologue), Sources is always populated. So VisitUnaryAddrMutDeref's missing empty-guard is unreachable — defensive asymmetry, not a defect. Verified: `&_Mut *mk(p)` (call, hoisted) clean; `&_Mut *p`,`&_Mut *(p)`,`&_Mut *&_Mut x` all populate Sources.

## ProcessDeref / RecursiveForFields dyn_cast-without-null-check (BSCBorrowChecker.cpp:308-309, 328-331) — LATENT, PROBED-INCONCLUSIVE 2026-05-29
**Latent defect (not reached)**: both sites do `dyn_cast<RecordType>(QT.getCanonicalType())`
then `RecursiveForFields(rt, ...)` WITHOUT a null check, after gating on `QT->withBorrowFields()`.
But `withBorrowFieldsImpl` (TypeBSC.cpp:23) returns true for NON-record types: a borrow-qualified
pointer (`int *_Borrow`, line 1) and an owned pointer to a borrow-bearing struct (`Inner *_Owned`,
derefs at line 30). For those, `dyn_cast<RecordType>(pointer)` = null → `RecursiveForFields(null)`
→ line 318 `RT->getDecl()` null deref → compiler crash (release build → SIGSEGV; would assert in
assert builds).
**Why not reached (probed)**: `GenerateImplicitAssign` (:336) gates Sources on `IsTrackedType`
(only owned). Borrow-only structs (`struct WB{int *_Borrow b;}`) aren't tracked → never reach
ProcessDeref (rg3 clean). Owned-value reassignment is rejected first ("assign to _Owned value",
rg1/rg2). Owned-ptr-to-borrow-struct DeclStmt move (rf3) → leak, ProcessDeref not invoked.
Could not construct a reaching trigger from in-scope valid code. Owned structs (which might reach
it) are OOS. **Not filed** — real code smell, but no reproducer in release. A maintainer with an
assert build, or a future owned-struct path, should add the null guard. `/tmp/rf*.cbs,rg*.cbs`.

## 2026-05-29 — RegionCheck core fully read + dump-driven soundness audit

Read the entire conflict/region/loan/liveness pipeline and verified it via
`-Xclang -dump-borrow-check` (regions.points = NLL extents; loans; actions).

**Pipeline** (RegionCheck::Check :1631): Liveness → PopulateInference (per live
point, `AddLivePoint` every region of a live borrow-typed var; per Borrow/Assign
action add outlives constraints to SuccPoints) → InferenceContext::Solve (fixed
point; DFS::Copy propagates Sup's points into Sub along CFG successors) →
LoansInScope (forward dataflow; a loan is KILLED at a point not in its region, or
when its path-prefix is overwritten — `LoansKilledByWriteTo` + `Action::OverWrites`
returning Dest) → BorrowCk (per point, `CheckAction` runs the in-scope loans
through CheckShallowWrite/CheckRead/CheckMutBorrow/CheckMove/CheckStorageDead).

**Conflict predicates** (all read, all sound):
- `FindLoansThatFreeze` (write/storage-dead) vs `FindLoansThatIntersect` (read/move/mut-borrow), via path `to_string()` prefix matching + `supportingPrefixes` (stops at `*r` for const-borrow).
- `FrozenByBorrowOf` :1537 — backward walk of prefixes; STOPS at `*p` when p is a non-owned pointer (writing p doesn't affect `*p`), CONTINUES through owned (writing owned p overwrites `*p`). Correct.
- `CheckMove` is strictest (FindLoansThatIntersect, no Depth gate); CheckStorageDead/CheckShallowWrite use Freeze.

**Interproc lifetime**: PreprocessForParamAndReturn conflates ALL borrow-typed
params into one shared `ParamRN` and pins a CAPPED free region to every CFG point;
runs ONLY when the return type is tracked. A borrow-returning CALL has the result
tied to ALL borrow args by Prologue emitting `result = each_borrow_arg` assigns at
the call point (confirmed in dump) — covers struct-by-value args with borrow
fields too. Sound (conservative).

**SOUNDNESS VERDICT: SATURATED-SOUND.** Probed (dump-confirmed, all correct):
return-of-local (ForStorageDead, region→End), struct-by-value-return w/ local
borrow field, loop back-edge carried conflict, array-elem var-index (→`*a`
collapse), interproc result/arg lifetime (direct + struct-field arg), NLL
precision (read-after-last-use clean), pointer-arith × borrow (`a+=1` while
`*a` borrowed → forbidden, matches Rust). See `_probed.md` 2026-05-29 entries.
No upstream commit in 984b1f6..a9deb1b changed this file; the Chain-C reopen on
this hop is from our own dump commit, not a behavior change.

### `VisitUnaryAddrMutDeref` empty-Sources (C1) — **PROBED-SHAPE-REJECTED 2026-05-29**
:774 indexes `Sources[0]` with no empty-guard; const sibling :748 guards
`Sources.empty()`. Real code asymmetry, but UNREACHABLE from valid _Safe: `_Borrow`
defaults `_Nonnull` (null-cast → nullability gate) and the `_Nullable` form is
hoisted by Prologue into a temp DRE before ActionExtract sees it, so Sources is
never empty (same Prologue-hoisting that rejected the subscript-index candidate).
Const guard is defensive. Not filable. `/tmp/crash_*.cbs`.

### `VisitReturnStmt` :709 / `VisitIncrementDecrementOp` :354 — read, no candidate
Return of tracked type → Assign with Dest=`__ret`, RNL=fresh free region; downgrades
to Init if RNR invalid (rare; call results get RNR via the post-Prologue temp DRE).
`x++` modeled as `x = use(x)` (LHS write + RHS read) — explains why `a++` conflicts
with a live borrow of `*a`. Sound.

## 2026-05-30 Explorer — ESCAPE-TO-LONGER-LIVED soundness surface (F81 motivating Q) — NEW FN found

Built the escape-form × caught/missed table for "store/return a borrow into a sink
that outlives the referent". Region-inference site: `PopulateInference` (:1569) +
`PreprocessForParamAndReturn` (:2170, early-return :2171-2172 when ret-type untracked)
+ the `Action::Assign` DerefRN relation (:1614-1622).

| escape form | shape | verdict |
|---|---|---|
| (a) return borrow of local | `return &_Mut x;` (with a borrow param so the Sema "return-needs-borrow-param" gate passes) | **CAUGHT** (`x does not live long enough`) |
| (c) inner-local → outer-local | `{ int inner; outer = &_Mut inner; } use(outer)` | **CAUGHT** |
| (d) local → outliving struct field (nested scope) | `{ int inner; h.f = &_Mut inner; }` h at outer scope | **CAUGHT** |
| (e) reborrow outlives original (nested) | `outer = &_Mut *mid;` mid borrows inner | **CAUGHT** |
| (b-raw) store thru `*out` (`int*_Borrow*` raw param) | `*out = &_Mut x;` | SEMA-BLOCKED (`'*' forbidden in safe zone`) |
| (b-struct) thru `struct H *_Borrow` param field | `s->f=&_Mut x;` | SEMA-BLOCKED (struct w/ borrow field can't be `_Borrow`-qualified) |
| **(b-owned) local → `_Borrow` field of `_Owned`-param/local-heap pointee, local at TOP LEVEL** | `s->f = &_Mut x;`, `int x` at fn top | **MISSED — ACCEPTED, runtime stack-use-after-return** |

### ROOT CAUSE (NEW FN, distinct from F11/F21/F24/F34/F38/F39/F42/F81)
A borrow of a **top-level function-local** stored into a `_Borrow` field reachable
through an **`_Owned` pointer** (param or local heap object) — `(*s).f = &_Mut x` —
is ACCEPTED by region inference. The byte-identical NESTED-scope twin
(`{ int x; s->f = &_Mut x; }`) is REJECTED. The discriminator is purely the scope
nesting of the borrowed local, NOT the return type.

**Why**: the dump (`-Xclang -dump-borrow-check`) of the accepted case shows
`ActionAssign: (*s).f RegionName {'region_0} = tmp RegionName {'region_1}` where the
SINK region `'region_0` (the DerefRN of the param-field write, created at :235/:337,
related at :1619-1620) gets points `[BB1/1..BB1/5]` — a finite in-body range that is
NOT pinned to the free/End region. A top-level local `x` lives to fn-end (StorageDead
at the LAST point), so `region(loan x) ⊇ region('region_0)` holds at every CFG point
inside `g` → outlives satisfied → accepted. The inference never models that `(*s).f`
(a field of caller-owned memory) OUTLIVES the function frame. The nested twin is
caught only INCIDENTALLY: `x`'s StorageDead lands mid-body so the point-set check
fails — i.e. region inference catches scope-escape but NOT frame-escape-via-aggregate.
`PreprocessForParamAndReturn` is the would-be fix site but (i) early-returns when the
return type is untracked, and (ii) even when it runs it pins only the param's TOP-LEVEL
region (`ParamRN`), never the DerefRN of a write THROUGH the param (`(*s).f`).

**Runtime**: `g` parks `&_Mut x` (g's top-level local) into the owned-param field,
round-trips ownership back to `main` via `__move_to_raw`/`__take_from_raw` (in-scope
library plumbing, dodges the owned-return gate + leak diag); `main` reads `*h->f`
after g's frame is reclaimed/clobbered → value is garbage (exit 1 / valgrind
"uninitialised value created by a stack allocation in main"). Repros in `/tmp`:
- static FN (ACCEPTED, should reject): `/tmp/F_escape_paramfield_static.cbs`
- one-line-diff baseline (nested local, REJECTED): `/tmp/F_escape_paramfield_baseline.cbs`
- runtime stack-use-after-return: `/tmp/F_escape_param_field_uaf.cbs`

**DISTINCT from**: F11 (BO_Comma visitor gap), F21 (move-through-`(*s).f` paren-deref),
F24 (callee-position read), F34 (OWNERSHIP analyzer leak FN — `IsTrackedType` doesn't
track `_Borrow`-to-owned struct; different analyzer, leak not dangle), F38 (`_Generic`
temp lifetime, OOS), F39 (array-elem subscript Kill), F42 (union mut-alias), F81/F82
(DECL-GATE placement, not region inference). This is a REGION/LIFETIME-INFERENCE
escape-modelling gap: the sink region of a write through an owned indirection isn't
pinned to outlive the frame. Defect class: **NEW** (region-inference frame-escape via
aggregate field) — closest existing label C5 (dataflow/region state hole) but the
mechanism (DerefRN not pinned to free region for param/owned-pointee writes) is new.
Severity **HIGH** (in-scope `_Safe` false negative → runtime stack-use-after-return).

### ⚠️ CORRECTION 2026-05-30 (main-thread re-validation) — FINDING REJECTED, region inference is SOUND
The above is an **over-claim** (same shape as the earlier cast-borrow-drop / `IsSafeFunctionPointerTypeCast` rejections). The "MISSED" cell (b-owned) is a **SOUND accept**, not a bug:
- The "static FN" `g(struct H *_Owned s){ int x=0; s->f=&_Mut x; safe_free(s); }` is accepted because it is **correct**: `safe_free(s)` consumes `s` (and its field `f`) **before** the top-level `x` dies at fn-end. So the borrow in `s->f` is valid for its entire lifetime — `x` outlives the free point. (Re-validated: `/tmp/rv_A_run.cbs` runs **clean** under vg_probe — ERROR SUMMARY 0, no leak, no invalid read.) The nested twin is rejected because there `x` dies *before* `safe_free(s)` — so the asymmetry is **correct**, not a gap.
- The actual ESCAPE in **pure `_Safe`** — return the owned `s` so its local-borrowing field outlives the frame — is **correctly REJECTED**: `_Safe struct H *_Owned g(struct H *_Owned s){ int x=0; s->f=&_Mut x; return s; }` → `error: 'x' does not live long enough` (`/tmp/rv_C.cbs`). Region inference catches the frame-escape.
- The explorer's runtime UAF only "worked" by laundering `s` through `__move_to_raw`/`__take_from_raw` (**`_Unsafe`**) — the intended raw-transfer escape hatch, which voids the borrow guarantee by design. Not a region-inference false-negative.
**Verdict: NOT FILED. The escape-to-longer-lived surface is SOUND** — forms (a)/(c)/(d)/(e) caught, (b-raw)/(b-struct) Sema-blocked, (b-owned) accept is sound (referent outlives the in-body free) and the real escape (return) is caught. Region inference models frame-escape correctly. Lesson: a sound in-body accept (freed-before-referent-dies) is not a bug just because a nested-scope variant is rejected for a different (also-correct) reason.

## 2026-05-30 E4 visitor-coverage — DefUse/ActionExtract handled-vs-unvisited AST-kind table

Goal (steering E4): find a DISTINCT unvisited AST kind where a borrow is CREATED or USED,
falls to VisitStmt (iterate children) → its def/use is never recorded → region constraint
never generated → checker accepts a dangling borrow. PURE `_Safe`, no `_Unsafe`/raw laundering.

**DefUse handled** (liveness, :65-90): BinaryOperator, BinAssign, CallExpr(args only),
DeclRefExpr, DeclStmt, MemberExpr, ReturnStmt, UnaryDeref, UnaryOperator. Everything else →
VisitStmt (recurse children, preserve Action state).
**ActionExtract handled** (:197-407): ArraySubscriptExpr, BinaryOperator, BinAssign, CallExpr,
CStyleCastExpr, DeclRefExpr, DeclStmt, InitListExpr, MemberExpr, ReturnStmt,
UnaryAddrConst/ConstDeref/Mut/MutDeref, UnaryDeref, UnaryExprOrTypeTrait, Unary{Pre,Post}{Inc,Dec},
IncrementDecrementOp. Everything else → VisitStmt.

**UNVISITED Expr kinds that can carry a borrow create/use** (candidate holes):
1. **ConditionalOperator** (`c ? &_Mut a : &_Mut b`, or `c ? b1 : b2` reading borrows) —
   Prologue `TransformConditionalOperator` (SemaDeclBSC:890) rewrites `?:`→IfStmt + temp. PROBED-prior
   (BSCBorrowChecker.md:48-50) at STATEMENT level → normalized. UNPROBED: a `?:` whose VALUE is a
   borrow assigned to a longer-lived sink (the temp's region may not be reconnected to BOTH arms).
2. **CompoundLiteralExpr** carrying a `_Borrow` field, address-of or member-read — no Visit; the
   DeclStmt special-cases it for INIT (:574) but a bare `(struct S){...}.f` read or `&_Mut (CL).f`
   is not. RHS-position only (Prologue may hoist).
3. **ParenExpr / ImplicitCastExpr** around a borrow read — fall to VisitStmt, recurse children,
   preserve op/Kind → benign (children pushed to Sources correctly). NOT a hole.
4. **AttributedExpr** (`__attribute__` on an expr) around `&_Mut x` — no Visit; rare in _Safe.
5. **ChooseExpr / GenericSelectionExpr / StmtExpr** — OOS (keyword list). DROP.

Ranked: #1 (?: value-flow reborrow, distinct from F11/F24) > #2 (compound-literal borrow field)
> #4. Probes below.

## 2026-05-30 E4 — CONFIRMED-NEW: DefUse::VisitMemberExpr base-skip on Action==None (discarded member-read)
**Root site**: `DefUse::VisitMemberExpr` (BSCBorrowChecker.cpp:147-156) — visits the base ONLY
`if (Action == Use)`. A top-level **bare/discarded member-access expression statement** (`b->f;`)
starts with `Action==None` (DefUse ctor :73) and never transitions to Use, so the base borrow `b`
is never recorded as a USE → liveness underestimates → the loan of the borrowed referent is
considered DEAD at that statement. `ActionExtract::VisitMemberExpr` mirrors the gap (the dump shows
the `b->f;` statement emitting `ActionUse:  use()` with an EMPTY source list).
**Consequence (FN)**: a conflicting op placed where `b`'s only remaining use is a discarded
member-read is wrongly ACCEPTED:
- `b=&_Mut s; s.f=99; b->f;` → ACCEPTED (write-while-mut-borrowed; should reject).
- `b=&_Mut *o; safe_free(o); b->f;` → ACCEPTED (free-while-borrowed; should reject `cannot move
  out of o because it is borrowed`).
**One-line asymmetry**: binding the read (`int z = b->f;`) makes it a VISIBLE use → both forms are
correctly REJECTED.
**DISTINCT from**: F11 (BO_Comma — VisitBinaryOperator does nothing for comma), F21
(`(*s).f` move getMemberFullField paren-strip), F24 (callee-position read — VisitCallExpr skips
`getCallee()`), F39 (array-subscript Kill), F42 (union alias). This is the **Action==None
member-BASE skip** for a value-discarded read-statement — a different visitor branch.
**Severity guess MEDIUM**: genuine borrow-checker soundness false-negative (free/write while
mut-borrowed accepted) with a clean one-line baseline and dump proof. Blast radius for a *runtime*
UAF is bounded: the only liveness-hiding read form (a value-discarded member-read) does NOT codegen
a load (dead-read elimination), and any OBSERVABLE read of `b` re-extends liveness backward and
catches the conflict — so a vg-confirmed dangle is not directly reachable from this exact hole. The
static rule is violated regardless. Defect class **C3** (Visit/branch coverage gap; same family as
F11/F24).
**Repros**: /tmp/E4_member_base_skip_FN.cbs (heap free FN, ACCEPTED) +
/tmp/E4_member_base_skip_baseline.cbs (one-line-diff REJECTED); stack variant
/tmp/explorer_probe.cVoF1j.cbs (write-while-borrowed FN) + /tmp/explorer_baseline.AxuvzW.cbs.
**Fix surface**: in `DefUse::VisitMemberExpr`, treat a member-access in a value-needed/None context
as a Use of the base (or have the Prologue not drop discarded reads); mirror in
`ActionExtract::VisitMemberExpr`.

## 2026-05-30 R4 callee-position lifetime — heap-free UAF variant of F24 (FOLD, but severity-falsifying)
**Site**: `DefUse::VisitCallExpr` (:118-122) + `ActionExtract::VisitCallExpr` (:470-478) —
both visit `CE->arguments()` only; `CE->getCallee()` is NEVER visited. `TransformCallExpr`
(SemaDeclBSC:858-876) hoists args + the whole CallExpr but NOT the callee subexpr. (= F24 site.)
**Probe**: heap referent, borrow live ONLY in the callee position, referent freed:
```
struct Box *_Owned o = safe_malloc<struct Box>(make());   // Box{ fn_t fp; int data; }
struct Box *_Borrow b = &_Mut *o;
safe_free((void *_Owned)o);          // frees *o
int r = (b->fp)(20);                 // callee-position read loads fp from FREED heap
```
**Outcome**: ACCEPTED by checker. Dump: the call action is `ActionInit: _borrowck_tmp_10 = use()`
with an EMPTY source list — `b`/`b->fp` not recorded as a use, so loan `'region_2` is dead at
`safe_free` → free accepted. Runtime: **valgrind `Invalid read of size 8` at `run`** (the b->fp
load), addr "0 bytes inside a block of size 16 free'd by safe_free". This is a REAL use-after-free,
NOT a no-op discarded read (contrast the REJECTED E4 discarded-member-read below).
**Asymmetry baseline (one-line diff)**: bind the read first — `fn_t c = b->fp; int r = c(20);` →
REJECTED (`cannot move out of \`o\` because it is borrowed`). The RHS-position read IS visited.
**FOLD decision**: root cause = SAME function, SAME dropped subexpression as F24; same fix (visit
`CE->getCallee()`). NOT a distinct root cause → **FOLDED-into-F24**, not separately filed.
**BUT**: this FALSIFIES F24's own severity rationale ("data accessed via a function-pointer callee
is just the pointer bits ... runtime impact is bounded ... no UB"). With a HEAP referent that is
FREED, the function-pointer-callee read is a genuine vg-confirmed UAF → F24 should be
**upgraded MEDIUM→HIGH**. F24 original repro still compiles clean vs binary 28656aa9 (still live).
Repro `/tmp/explorer_probe.H26da5.cbs`; baseline `/tmp/explorer_baseline.7Gk2BW.cbs`; ledger `/tmp/probed_R4E4.md`.

### ⚠️ E4 CORRECTION 2026-05-30 (main-thread re-validation) — FINDING REJECTED, sound dead-borrow accept
The E4 "DefUse::VisitMemberExpr base-skip when Action==None" finding is an OVER-CLAIM (same shape as the region-escape rejection above). A discarded top-level member-read `b->f;` codegens to NOTHING (dead-read elimination), so when it is a borrow's LAST use, the borrow is genuinely DEAD — allowing a conflicting `safe_free`/write is SOUND (no actual use-after-free). Re-validation: `/tmp/E4_member_base_skip_FN.cbs` runs CLEAN (exit 0, valgrind 0 errors). And when a REAL use follows the discarded read (`b->f; int z = b->f;` after the free), the checker CORRECTLY REJECTS ("cannot move out of `o` because it is borrowed") — the real use extends liveness. So the bound-vs-discarded asymmetry the explorer flagged is CORRECT behavior: the bound form genuinely UAFs (reject), the discarded no-op form does not (accept). NOT a bug, NOT filed. (The pre-existing note at the `DefUse::VisitMemberExpr` Action==None bullet already called this "harmless" — that assessment stands.) Lesson reinforced: a static accept is not a soundness FN unless a PURE-_Safe runtime exploit (vg dangle) exists; a no-op last-use read is a dead borrow.

## ActionExtract::VisitBinaryOperator (BSCBorrowChecker.cpp:426-446) — DUPLICATE of F11, RETRACTED 2026-06-04 (was mis-filed F93)

**Invariant**: every borrow action (&_Const/&_Mut/reborrow) inside an expression must be extracted so
region inference can constrain the referent's lifetime to the borrow. ActionExtract walks the expr tree;
VisitBinaryOperator must visit operands of any BinaryOperator that can CONTAIN a borrow sub-expression.

**Peers**: BSCOwnership.cpp VisitBinaryOperator (:2177 — DOES special-case BO_Comma, op=GetAddr on LHS);
BSCNullabilityCheck getExprPathNullability BinaryOperator case (F92 — omits pointer arith); the F91/F92/F93
family = form-based visitors with opcode/shape coverage holes around comma + pointer arithmetic.

**Root cause (C2 opcode-switch hole)**: dispatch covers ranges BO_Mul..BO_Shr, BO_LT..BO_NE,
BO_MulAssign..BO_OrAssign. **BO_Comma is omitted** → operands never visited → `(0, &_Mut x)` launders the
borrow → referent untracked → free/move/mutate-while-borrowed undetected → runtime UAF.

**Candidates**:
1. Borrow action in a comma `(0, &_Mut *o)` then free → **PROBED-folded-into-F11** (mis-filed as F93, retracted) (valgrind Invalid read at
   use; bare form rejects "cannot move out of `o` because it is borrowed").
2. DefUse::VisitBinaryOperator (:93) — does the DEF/USE (liveness) pass also skip comma operands? If so the
   liveness of `x` in `(0, &_Mut x)` is also wrong (compounding). UNPROBED — likely same fix family.
3. Other ActionExtract opcode gaps: BO_Assign goes through VisitBinAssign (ok); but is there any borrow
   reachable through BO_PtrMemD/I or an opcode outside the three ranges? UNPROBED (C-subset limits this).

## static-local borrow region tracking (BorrowCk × storage duration) — probing (F95 sibling-analysis)
**Invariant**: a borrow stored into a variable of static storage duration must
outlive that storage — borrowing a function-local (region = fn body) into a
`static int *_Borrow p` (region = 'static) must be REJECTED (the local dies at
return; static p dangles on the next call → use-after-scope).
**Peers**: F95 (owned-leak analysis skips static-locals — SAME storage-duration
theme, DIFFERENT analysis); region inference / Liveness in BSCBorrowChecker.
**Candidates**:
1. **`static int*_Borrow p; p=&_Mut x;` (x = param) → PROBED-FOLDED-F95**.
   Non-safe: compiles clean, runtime `*p` reads a dead stack slot on re-entry
   (use-after-scope). Same root as F95 — the borrow declaration gate
   (CheckBorrowOrIndirectBorrowType) isn't called for static-locals (global IS
   rejected). FOLD: the global site calls both owned+borrow gates, so F95's fix
   (mirror the global site at the static-local site) closes this too. Strengthens
   F95 to leak+UAF. (In _Safe, static borrow rejected as "mutable global".)
2. thread_local borrow variant. UNPROBED.
3. static borrow of a GLOBAL (`p=&_Mut g`) — should be SOUND (global outlives). control.

## CheckBorrows Shallow vs Deep (BSCBorrowChecker.cpp:1361-1410) — write-conflict granularity probe
**Invariant**: a write to a path conflicting with a live borrow must be rejected.
Shallow (`FindLoansThatFreeze` :1366) is used for assignment-dest writes
(:1292/1305/1316); Deep (`FindLoansThatIntersect` :1369) for CheckDeepRead/Write.
Freeze ⊆ Intersect, so a Shallow write-check could MISS an intersecting super/sub-path loan.
**Peers**: FindLoansThatFreeze/Intersect (F42 path-identity), CheckShallowWrite (:1443),
CheckDeepWrite (:1432). Loop returns on FIRST conflict (:1388/1406 — diag-complete, sound).
**Candidates**:
1. **whole-struct write while a FIELD is mut-borrowed — Shallow misses super-path? — probing**.
   `&_Mut s.a` then `s = {...}` overwrites s.a; if the shallow dest-check only freezes
   the exact path, the super-path write is missed = HIGH FN (write through live borrow).
2. sub-path: borrow whole `&_Mut s`, write field `s.a` — symmetric. UNPROBED.
3. array elem: `&_Mut arr[0]` then write `arr[0]` via different index expr. UNPROBED.

## CheckMove / CheckStorageDead (BSCBorrowChecker.cpp:1422-1456) — move-vs-borrow restriction probe
**Invariant**: `CheckMove` (FindLoansThatIntersect, :1423) rejects moving a path if
IT, a SUBPATH, or a PREFIX is borrowed (stricter than write/storage-dead).
`CheckStorageDead` (FindLoansThatFreeze, :1452) rejects scope-free only if interior
data borrowed (allows `*var` borrowed). Comment :1412-1421 states the contract.
**Peers**: FindLoansThatIntersect/Freeze (F42 path-identity), CheckMutBorrow/Read.
**Candidates**:
1. **move WHOLE struct while a SUBPATH (s.b) is borrowed → must REJECT — probing**.
2. move while union-sibling borrowed → FOLD-F42 (path-identity).
3. StorageDead allows `*var` borrowed but rejects `var` borrowed — asymmetry sound? UNPROBED.

## two-arg aliased mutable borrow (safe_swap(&_Mut x, &_Mut x)) — probing
**Invariant**: passing two `&_Mut` of the SAME location as separate call args must be
rejected (two simultaneously-live exclusive mutable borrows of one place).
**Peers**: CheckMutBorrow (cycle 8/16), F42 (path-identity alias), CheckBorrows.
**Candidates**:
1. **`safe_swap(&_Mut x, &_Mut x)` — two &_Mut of x as args → REJECT? — probing**.
2. `&_Mut x` + `&_Const x` as two args (mut+shared conflict). UNPROBED.
3. aliased via fields `safe_swap(&_Mut s.a, &_Mut s.a)`. UNPROBED.

## borrow returned through a call — lifetime propagation — probing
**Invariant**: a `_Borrow` returned from a function (tied to a borrow param) retains
its constraint on the original; writing the original while the returned borrow is
live must be REJECTED (else the returned borrow dangles/aliases).
**Peers**: CheckMutBorrow, return-borrow signature rule (cycle 8), F39 (borrow DefUse).
**Candidates**:
1. **write x while `b=get(&_Mut x)` (returned borrow) is live → REJECT? — probing**.
2. returned borrow outlives via nested call. UNPROBED.
3. returned _Const borrow + write. UNPROBED.

## returned borrow from EITHER of two params (conservative region union) — probing
**Invariant**: a `_Borrow` returned via `c ? a : b` must conservatively constrain
BOTH source params; writing either while the returned borrow is live = REJECT (the
borrow might alias either). Tying to only one (or none) = FN.
**Candidates**:
1. **`r = pick(&_Mut x, &_Mut y)`; write x → REJECT? and write y → REJECT? — probing**.

## borrow loan-set capacity (breadth sweep) — probing
**Invariant**: a write conflicting with the FIRST of N simultaneously-live borrows
must be caught regardless of N; the loan-set must not cap/evict early loans (FN).
**Peers**: FindLoansThatFreeze/Intersect, move-breadth (sound), owned-field-breadth (sound).
**Candidates**:
1. **write x0 while b0 + N-1 other borrows live → caught at all N? — sweeping**.

## SafeExpr in borrow use-extraction (DefUse, F62 sibling) — probing
**Invariant**: a SafeExpr-wrapped use of a borrow `*(_Safe(b))` must register as a
use of b (keeping it live); else a conflict BEFORE that use is missed (FN). The
DefUse/ActionExtract sites use plain IgnoreParens (no SafeExpr strip).
**Candidates**:
1. **write x while b live, last use is `*(_Safe(b))` → caught? — probing** (FN if SafeExpr drops the use).

## shared + exclusive borrow coexistence — probing
**Invariant**: a `_Const` (shared) and `_Mut` (exclusive) borrow of the SAME place
cannot be simultaneously live; must REJECT (shared XOR exclusive).
**Candidates**:
1. **`&_Const x` + `&_Mut x` both live → REJECT? — probing**.
2. two `_Const` of same x → ACCEPT (shared allows multiple). control.

## borrow stored in a struct field — lifetime tracking — probing
**Invariant**: a `_Borrow` stored in a struct field retains its constraint on the
borrowed-from; writing the original while the field-borrow is live must REJECT.
**Candidates**: 1. `struct B{int*_Borrow f;} b={&_Mut x}; x=5; *b.f` → REJECT?

## struct-with-borrow-field RETURN escape — probing
**Invariant**: returning a struct holding a `_Borrow` of a LOCAL must be REJECTED
(the return-borrow signature rule / escape must cover borrow-in-struct-FIELD, not
just top-level return types) — else the borrow dangles past the local's scope.
**Candidates**: 1. `struct B{int*_Borrow f;} make(int x){ return (B){&_Mut x}; }` → REJECT?

## NLL loop-reborrow precision (FP hunt) — probing
**Invariant**: a borrow re-created each loop iteration (used + dead within the
iteration) must NOT conflict with the next iteration's borrow — NLL must end it at
the iteration boundary. Over-conservative = FP (idiomatic loop-fill rejected).
**Candidates**: 1. while loop borrowing `arr+i` each iter, write *e → ACCEPT (no FP)?

## ActionExtract Visit* coverage (C3) — note
ActionExtract (BSCBorrowChecker.cpp:197+) has thorough UnaryOperator breakdown
(AddrConst/AddrConstDeref/AddrMut/AddrMutDeref/Deref/PostDec/PostInc/PreDec/PreInc) +
ArraySubscript/CStyleCast/InitList/BinAssign/Call/Member/Return/DeclStmt. NO explicit
VisitConditionalOperator → falls to VisitStmt (children). UNPROBED: borrow action through
`c?a:b` arms — does the VisitStmt-children fallback extract the borrow correctly?

## region-inference source-read (2026-06-08) — no FIXME risk sites, behaviorally sound
Core fns: AddLivePoint/DFS::Copy/Liveness::SimulateBlock/LoansInScope/FindLoansThatFreeze
/Intersect/PopulateInference. NO FIXME/TODO/conservative-gate risk sites (unlike BSCIRBuilder
:574=F98). Behaviorally validated sound this session (shared/exclusive, field-level conflict,
reborrow freeze, lifetime-through-call, conservative region-union, loan-set uncapped, escape,
conditional-move). The F98-method (implementer-flagged incompleteness) has no purchase here.

## LoansKilledByWriteTo (:1176) — read, SOUND (modulo F42 aliasing)
INVARIANT: writing to path W kills every loan L where W is a prefix of L (L is at-or-below
W in the path tree) — i.e. reassigning `a.b.c` kills loans on `a.b.c` and sub-paths `a.b.c.d`,
since the path no longer evaluates to the same thing. Parent loans (loan on `a.b` when writing
`a.b.c`) are NOT killed (correctly — they're conflicts, handled by FindLoansThatIntersect, not kills).
PEERS: LoansKilledByWriteTo ← SimulateBlock (:1194) ← LoansInScope; FindLoansThatFreeze/Intersect.
GAP: path comparison is string-based (`p->to_string() == path->to_string()`) → does NOT see two
paths aliasing through pointers (e.g. `*p` vs `*q` where p==q) — the known F42 union/alias surface.
No NEW defect: direct-path kill logic is correct; aliasing is F42 (filed).

## FindLoansThatIntersect (:1507) + FrozenByBorrowOf (:1536) — read, SOUND (modulo F42)
FindLoansThatIntersect: BIDIRECTIONAL conflict — accessing `a.b.c` intersects loans on ancestors
(`a.b.c`/`a.b`/`a`, via path's prefixes==loan.path) AND descendants (`a.b.c.d`, via
loan.path's supportingPrefixes==path). Broader than the kill logic (which is at-or-below only).
FrozenByBorrowOf: walks the path backwards collecting freeze-invalidating paths; KEY (:1550) — for
`*r` where r is borrow/raw (pointer, NOT owned), writing r doesn't affect *r's memory → stops; for
OWNED r it continues (moving r invalidates *r, pointee tied to pointer). Correct owned-vs-borrow split.
Both use string `to_string()` comparison → same F42 aliasing gap. Direct-path logic sound.
BORROW-CHECKER CORE LOAN LOGIC now fully read: Kill/Intersect/Freeze all sound modulo F42 aliasing.

## LiveRegions (:1094) — read, PLUMBING (no gap)
Maps a LivenessFact (set of live VarDecls) → set of live RegionNames, filtering to IsTrackedType
(borrow/owned) vars only. Non-tracked live vars contribute no region (correct). Feeds region
inference / loan-conflict detection. No candidate. BORROW-CHECKER now comprehensively read:
LoansInScope/Kill/Intersect/Freeze (core loan logic) + Liveness/LiveRegions (region liveness).

## CheckBorrows/CheckShallowWrite/CheckMutBorrow/CheckRead/CheckMove (:1396-1480) — candidates 2026-06-17
INVARIANT: write/storage-dead use Shallow(FindLoansThatFreeze=path+prefixes); read/mut-borrow/move use Deep(FindLoansThatIntersect). Comment(:1453): writing `x` (holding &mut) while `*x` borrowed is ALLOWED (overwrite kills old provenance) — but moving x is not.
Candidates:
1. [core soundness] write `x = v` while `x` is mut-borrowed (`b=&_Mut x`) → must emit ForWrite; FN if accepted (use-after-mutate). **UNPROBED** (top)
2. [shallow/deep gap] the :1453 "write x while *x borrowed allowed" relaxation — sound only if overwrite truly kills the *x loan; deref-prefix freezing in FrozenByBorrowOf is the crux. UNPROBED (BSC borrow-of-borrow restriction may make unreachable)
3. [move vs write asymmetry] move x while *x borrowed must be forbidden (Deep) while write allowed (Shallow) — verify asymmetry holds. UNPROBED

## FindLoansThatIntersect/FrozenByBorrowOf (:1542/:1572) — candidates 2026-06-17
INVARIANT: deep access `a.b.c` intersects loans on prefixes(a,a.b,a.b.c) + extensions(a.b.c.d). FrozenByBorrowOf walks UP: borrow `*r` freezes r ONLY if r is OWNED (reassign frees old pointee→dangling); non-owned borrow r → STOP (old pointee outlives r, reassigning r safe).
Candidates:
1. [owned/non-owned distinction] reassign non-owned borrow r while *r borrowed by b, use b → must be ALLOWED+sound (b→old pointee outlives). FP if rejected; FN-soundness if old pointee could dangle. **UNPROBED** (top)
2. [extension intersect] access a.b while a.b.c.d borrowed → must conflict. UNPROBED

## CheckAction dispatch + LoansInScope loan-Gen/Kill ordering (:1318 / :1229 / :1143) — read 2026-06-18 UNPROBED
**Invariant**: each CFG-point Action routes to the right Check* with the right Mode. Ordering in
SimulateBlock per point: (1) Kill loans not-in-region (LoansNotInScopeAt), (2) callback = run
BorrowCk conflict check, (3) Gen new loans, (4) Kill loans overwritten by this action's OverWrites().
The conflict check (step 2) precedes the OverWrites-kill (step 4) → a write that conflicts with a
live loan is caught BEFORE the loan dies. SOUND ordering by reading.
**CheckAction routing**: Assign→ShallowWrite(Dest)+Read(Source)+DerefSources(const?Read:MutBorrow);
Borrow→ShallowWrite(Dest)+(Shared?Read:MutBorrow)(Source); Init→ShallowWrite(Dest)+Sources
(owned||moveSemantic?Move:Read)+DerefSources; Use→Uses(owned||moveSemantic?Move:Read)+DerefSources;
StorageDead→CheckStorageDead. The owned||isMoveSemanticType pair (vs F79's owned-only) is the FIXED
form here — no F79 gap on the BorrowCheck side.
**LoansInScope ctor (:1143)**: explicit loans (ActionBorrow→region=getRegion(RNR)); implicit
deref-loans from Assign(region=getRegion(DerefRN)) / Use+Init(region=getEmptyRegion). Empty-region
deref-loans (Use/Init) → MayContain always false → killed at every point except their gen point
(point-loans = instantaneous reads). Intentional.
**Peers**: FindLoansThatFreeze (Shallow), FindLoansThatIntersect (Deep), FrozenByBorrowOf, LoansNotInScopeAt.
**Candidates**:
1. [super-path shallow write] whole-struct write `s = {...}` while field `s.a` mut-borrowed → must
   REJECT (ForWrite). FrozenByBorrowOf(s.a) returns [s.a, s] (s is a struct, not a ptr → walk
   continues to Var s) → FindLoansThatFreeze matches `s` → reject. Reasoned-SOUND; probe to confirm. UNPROBED
2. [Init Sources Move-vs-Read] a struct value with a `_Borrow` field used as Init Source (not
   owned, not move-semantic) → CheckRead not CheckMove. Reading a borrow-bearing struct value while
   its referent conflicts — does the deref-loan get generated? UNPROBED
3. [empty-region deref-loan] a read-through-borrow `*b` used as an Init/Use DerefSource has
   empty region (point-loan). Two reads of `*b` at different points → no held loan between them.
   Sound for reads (instantaneous), but verify a WRITE conflict at the read point still fires. UNPROBED

## EnsureBorrowSource / supportingPrefixes (:2166 / header:214) — read 2026-06-18 PROBED-SOUND-by-reading
**Invariant**: a reborrow `b = &_Mut path` where `path` traverses a borrow var `r` must add an
outlives constraint `b ⊇ r` (region) so `b` cannot outlive `r`. EnsureBorrowSource walks
SourcePath->supportingPrefixes(); for each Extension prefix whose `base` is borrow-qualified, adds
AddOutLives(BorrowRV, RefRV). Var prefix → return (no constraint; lifetime via plain liveness).
**supportingPrefixes** stops recursion at `*r` when `r` is a CONST borrow (you can copy `*r` to a
temp, so `r` itself needn't stay valid). For a MUT borrow it walks through to `r`.
**Reasoned-SOUND**: even for const-borrow `(*r).f`, the `*r` prefix IS in the list and its base `r`
IS checked (isBorrowQualified true) → constraint added. Reborrow through an OWNED-pointer deref
(`&_Mut o->f`, o owned) gets NO region constraint here, but the loan-conflict side (FrozenByBorrowOf
CONTINUES through owned) catches free/move-of-o-while-borrowed. Region side handled by loan side. No gap.

## BSC borrow checker is LIVENESS-BASED (NLL) — key semantic fact (2026-06-22, from G16 retraction)
- A loan/borrow region = the set of program points where the borrowed reference is LIVE (used later). A direct write to a borrowed variable conflicts with the borrow ONLY while the borrow is LIVE at the write point. Verified: `int*_Borrow b=&_Mut x; int r=*b; x=9;` (write AFTER b last use) = ACCEPTED; `x=9; return *b;` (write BEFORE b use) = REJECTED.
- Consequence: `DFS::Copy` (:1021-1026) NOT extending a loan region through a no-successor `noreturn` leaf is CORRECT — the borrow is not live past a `noreturn` call. A "write-while-borrowed accepted on a noreturn path" is NOT a bug (the borrow is dead there). This retired the false G16 finding. When auditing the borrow checker, judge against NLL/liveness, NOT lexical borrow scope.

## Chain-K AMENDMENT (2026-06-22): runOnBlock allowlist also breaks use-after-move READ detection
- The runOnBlock (:2613-2626) allowlist (DeclStmt/CallExpr/assignment-BO/inc-dec-UO/ReturnStmt) skips bare PURE-expression statements, so HandleDREUse never runs on them: `*p;` / `(void)*p;` after `safe_free(p)` is a use-after-move READ accepted in _Safe (baseline `sink=*p;` correctly rejected). SAME allowlist root as F20 (which documented the leak-side skip of a bare CompoundLiteralExpr). Prior Chain-K "runOnBlock SOUND" (move/leak-hoist) is amended: NOT sound for pure-expr reads. One fix (visit all stmts) covers F20 leak + this use-after-move-read. Folded into F20; not separately filed.

## Path index-precision (post-rewrite 53bc93dd/908ddef2 "differentiate *p and p[]") — Path::to_string/structurallyEquals (BSCBorrowChecker.h:228-252) + VisitArraySubscriptExprOrUnaryDeref (:808-846) — read 2026-06-23 — UNPROBED
**Invariant**: borrow-conflict detection must (a) treat DIFFERENT constant indices as DISJOINT (`&_Mut arr[0]` + `&_Mut arr[1]` = no conflict — else FP), and (b) treat VARIABLE indices CONSERVATIVELY (`arr[i]`+`arr[j]` could alias → must REJECT — else aliasing FN).
**What the rewrite actually changed**: the −435-line rework added a per-Path `UsesArraySubscriptNotation` flag (:824/:840 set when `FromArraySubscript && BaseTy.isBorrowQualified() && isArrayElemQualified()`). It is used ONLY in `to_string()` (:242-243: print `base[]` instead of `*base`). The conflict comparators (FindLoansThatIntersect :1509/:1516, FrozenByBorrowOf :1549/:1556, LoansKilledByWriteTo :1219) ALL switched from string-compare to `structurallyEquals` (:230) — which the comment (:228) EXPLICITLY says "Ignores UsesArraySubscriptNotation, which affects display only".
**Key fact**: the INDEX VALUE is NEVER stored in the Path. `arr[0]`, `arr[1]`, `arr[i]`, `arr[j]`, `*arr`, `p[0]` ALL build the same deref-Path of base `arr`/`p` (fieldName "*"). `structurallyEquals` only compares type + fieldName + base recursively → ALL element borrows of one base collapse to one path. So the rewrite is "differentiate `*p` vs `p[]` IN DISPLAY ONLY"; it did NOT add index precision.
**Peers**: F39 (FIXED — DefUse::VisitArraySubscriptExpr now at :189 visits base+idx as Use); F42 (union sibling path-identity FP-of-aliasing on the FN side); F36 (`&_Mut "lit"[i]` Sema gate).
**Candidates**:
1. (FP — disjoint constant elements) `&_Mut arr[0]` + `&_Mut arr[1]` both live → structurallyEquals collapses → REJECTED as "borrow `arr[]` more than once". Over-conservative FP (usability). Whole-array `_ArrayElem` unified-borrow may be DOCUMENTED INTENT (cf. cycle-12 note line 220 "arrays have unified-element borrowing per `_Borrow _ArrayElem` semantics") → likely SHAPE/INTENT not a filable bug. Probe to confirm whether it's intentional.
2. (FN — variable indices) covered: collapse means `arr[i]`+`arr[j]` ALSO map to same path → conservatively REJECTED. SOUND direction (no aliasing FN here).
3. (the real risk) does the collapse hold across the `*p` vs `p[]` BOUNDARY? `&_Mut *p` (UnaryDeref, FromArraySubscript=false → flag stays false) vs `&_Mut p[0]` (FromArraySubscript=true → flag true). structurallyEquals ignores the flag so they SHOULD collapse — but if any comparator accidentally used `to_string()==` instead, the `[]`-vs-`*` rendering diff would make them MISCOMPARE → aliasing FN. Already probed sound this session per steering. Re-verify no comparator regressed to string-compare.

## VisitInitListExpr array-elem path vs VisitArraySubscriptExprOrUnaryDeref (BSCBorrowChecker.cpp:651-677 / 808-847) — UNPROBED

**Invariant**: the path the borrow-checker assigns to array element `arr[i]` at
INIT-LIST time (`int *_Borrow arr[N] = {a,b}`) must UNIFY (via `structurallyEquals`)
with the path it builds for the SAME element accessed via SUBSCRIPT (`arr[i]`), so
that a move/borrow recorded at init-list granularity conflict-detects against a
later subscript move/borrow of the same element.

**The seam**:
- Init-list element path (line 662-665): `Extension{ base=arr, fieldName = "[" + idx + "]" }`
  → fieldName literally `"[0]"`, `"[1]"`. (numeric-index field, NOT a deref)
- Subscript access path (line 821-825 / 836-841): `Extension{ base=arr, fieldName="*" }`
  with `UsesArraySubscriptNotation=true`. → fieldName `"*"` (deref-like).
- `structurallyEquals` (BSCBorrowChecker.h:230) compares `fieldName` strings EXACTLY
  and ignores `UsesArraySubscriptNotation`. So `"[0]" != "*"` → init path and
  subscript path NEVER unify, regardless of index.

**Peers**: VisitInitListExpr struct-branch (uses real `Field->getName()` — symmetric
with MemberExpr's `getMemberNameInfo`, so struct case is SOUND). The array-branch is
the asymmetric sibling: init uses numeric-index strings, access uses "*".
`FindLoansThatFreeze`/`FindLoansThatIntersect` (1500/1542) both rely on
`structurallyEquals` over these paths.

**Candidates**:
1. (top) Init-list borrow of one element creates a loan keyed `arr.[0]`; a later
   subscript reborrow/move of `arr[0]` builds path `*arr` → no structural match →
   conflict MISSED (FN: double-borrow/aliasing not detected). reachability: needs an
   array of `_Borrow` initialized by list, then a subscript op on an element.
2. Disjoint-element FP: borrow `arr[0]` via subscript (path `*arr`, index erased)
   then access `arr[1]` (also `*arr`) → subscript collapses ALL indices to the same
   `*arr` path → could FP-conflict two disjoint elements. (this is the known-sound
   "subscript collapses to *arr" already probed — variable-index conservatism.)
3. Move asymmetry: `_Owned arr[N] = {mk(),mk()}` init at `arr.[0]`/`arr.[1]`, then
   subscript-move `consume(arr[0])` → moved-state recorded at `*arr`, not `arr.[0]`
   → init-tracked owned element & subscript-move disagree → double-free/leak (runtime).

## Multi-dim `_ArrayElem` Path build (BSCBorrowChecker.cpp:808-846, Path::to_string/structurallyEquals BSCBorrowChecker.h:230-252) — UNPROBED → see 2026-06-23 cycle below

**Invariant**: a borrow of `m[i][j]` (element-of-element) and a borrow of `m[0]` (a whole row)
that overlap in memory should be detected as conflicting; disjoint borrows accepted (or
conservatively over-rejected, the documented `_ArrayElem` unified-element intent).

**Path build for `int m[2][2]`**:
- `m[0]` (inner subscript, result `int[2]`) → Path("*", base=m), isBaseArrayType()→true → to_string()="m[]"
- `m[0][0]` (outer subscript, result `int`)  → Path("*", base=m[0]) → "m[][]"
- structurallyEquals ignores index value AND UsesArraySubscriptNotation → ALL of m[0][0],m[0][1],m[1][0],m[1][1] collapse to "m[][]"; all of m[0],m[1] collapse to "m[]".

**Peers**: FindLoansThatIntersect (:1542), FrozenByBorrowOf (:1571), prefixes()/supportingPrefixes().

**Candidates**:
1. Row-vs-element overlap: `&_Mut m[0]` (path m[]) + `&_Mut m[0][0]` (path m[][]). The element
   path has m[] as a STRUCTURAL PREFIX. Does conflict detection walk prefixes() and see the overlap?
   If FindLoansThatIntersect compares only the leaf path string (m[] != m[][]) → FN (two mut borrows
   of overlapping memory). HIGH if reachable. (reachability: needs &_Mut on an array-type lvalue.)
2. Element-of-element disjoint over-collapse (m[0][0] vs m[1][0]) → all "m[][]" → over-reject = FP =
   documented intent, not filable (mirror of 1-D constant-disjoint).
3. Variable-index 2-D `m[i][j]` vs `m[k][l]` → also "m[][]" → over-reject = intent.

### UPDATE 2026-06-23 — CONFIRMED-new FP at subscript reassign loan-kill (candidate 1 family, distinct root)

**Verdict**: NOT the structurallyEquals init-string seam directly. The real defect:
reassigning an array element via subscript (`arr[0] = &_Mut y`) does NOT durably end
the loan the element previously held when `arr[0]` is USED again afterward. A fresh
borrow of the original source (`&_Mut x`) is then wrongly rejected as
"cannot borrow x as mutable more than once at a time" (FALSE POSITIVE).

**Asymmetry**: identical NLL pattern with a SCALAR holder (`p=&_Mut x; p=&_Mut y;
&_Mut x; use1(p)`) compiles CLEAN. Only the array/subscript element holder FPs.

**Root**: subscript LHS dest path collapses to `*arr` (UsesArraySubscriptNotation,
VisitArraySubscriptExprOrUnaryDeref:819-825). ActionBorrow/ActionAssign `OverWrites()`
returns this collapsed `*arr`. The reassign's loan-kill cannot remove the prior
element-loan because the LATER subscript USE of arr[0] (also path `*arr`) re-extends
liveness to every loan ever tied to `*arr` — including the dead x-loan. The collapsed
path can't distinguish "arr[0] before reassign" from "arr[0] after reassign".

**Crucial isolating probe**: dropping the post-reassign `use1(arr[0])` makes it CLEAN.
So FP is triggered specifically by a subscript USE of the element after its reassign.

**Distinct from F39**: F39 is a false NEGATIVE (subscript-WRITE kills _Borrow liveness
→ accepts aliasing; fixed via DefUse::VisitArraySubscriptExpr). This is the opposite
direction — a false POSITIVE where subscript reassign+use keeps a dead loan live.

repro /tmp/explorer_repro.RYOFTW.cbs ; baseline /tmp/explorer_baseline.P2icOz.cbs

## Liveness::SimulateBlock / LoansInScope::SimulateBlock CFG-element iteration (BSCBorrowChecker.cpp:1058-1108, 1229-1277) — read 2026-06-23, PROBED-SOUND (F111-analog HYPOTHESIS REFUTED)

**Hypothesis tested**: ownership `runOnBlock` (BSCOwnership.cpp:2647-2657, just-filed F111) has a
top-level-kind allowlist (DeclStmt | CallExpr | assign-BO | inc/dec-UO | ReturnStmt) that SKIPS a
bare expression statement, leaving a use-after-move READ un-diagnosed. Does the BORROW checker have
an analogous block-iteration allowlist that would skip a bare expr-statement USING a `_Borrow`?

**Invariant (source-confirmed)**: NO such allowlist exists at the block-iteration level. Both
`Liveness::SimulateBlock` (:1069-1093) and `LoansInScope::SimulateBlock` (:1239-1276) iterate
EVERY `CFGStmt` via `for (CFGBlock::const_iterator it = Block->begin(), ei = Block->end(); it!=ei; ++it)`
with no top-level-kind filter — each is handed to `DefUse(S)` (liveness Gen/Kill) and to the action
map (LoansInScope conflict check). Dispatch happens *inside* `DefUse`/`ActionExtract`'s `Visit*`
methods (which fall through to `VisitStmt` = iterate children for unhandled kinds), NOT at the
block level. Contrast F111 where `OwnershipImpl::runOnBlock` gates `TF.Visit(S)` on the 5-kind filter.

**Trace of bare `*b;` (b = `int *_Borrow`) — BOTH visitors handle it**:
- `DefUse`: top-level `UnaryOperator(UO_Deref)` → `VisitUnaryDeref` (:173) sets `Action=Use`, `Visit(b)`
  → `VisitDeclRefExpr` (Action=Use) pushes `b` into `uses` → `Liveness::SimulateBlock:1090` `Gen(b)`.
  The borrow IS counted as a use for NLL. (No `runOnBlock`-style gate to skip it.)
- `ActionExtract`: `VisitUnaryDeref` (:849) → `VisitArraySubscriptExprOrUnaryDeref` (:808). `Kind==Noop`
  (:810) → sets `Kind=Action::Use`, `op=RHS`; visits `b`, builds `Src` path `*b`, `pathDepth==0` →
  pushes to `Sources` (:844-845). The use IS recorded as an `ActionUse` with Source `*b`.

So the F111-analog gap (block-level filter skipping a bare expr-stmt) does NOT exist here. This is
WHY the prior bare-borrow-exprstmt probes (_probed.md:1558-1564) all behaved correctly:
`(void)(&_Mut x);` CLEAN; `(void)(&_Mut a); int *_Borrow b1=&_Mut a; ...` CORRECTLY DIAGNOSED
"cannot borrow as mutable more than once"; `const int *_Borrow c1=&_Const a; (void)(&_Mut a);`
CORRECTLY DIAGNOSED. A discarded borrow read is correctly modeled (liveness Gen), and NLL correctly
ends the loan there if it's the last use — the same reasoning that retracted the E4 finding.

**Peers**: F111 (BSCOwnership runOnBlock filter — different file, different analyzer, the gap that
DOES exist there does NOT exist here); F11 (DefUse BO_Comma hole — a *per-Visit-method* opcode gap,
not a block-level filter); F20 (owned runOnBlock filter, leak-side).

**Conclusion**: FOLDED into the prior E4-retraction reasoning + the bare-borrow probes at
_probed.md:1558-1564. The borrow checker has no F111-analog block-level allowlist; bare
expr-statements using a `_Borrow` are processed by both DefUse and ActionExtract. No new root cause.
(Confirmatory probe re-run on fresh bin 34e6f26e below — bare `*b;` use-after-free-attempt still
correctly diagnosed, no FN.)

## Loan NLL-kill at NON-scalar / NON-subscript reassign sites — reborrow / struct-field-holder / loop-carried (BSCBorrowChecker.cpp:1211 LoansKilledByWriteTo + PopulateInference NLL regions) — read 2026-06-23, UNPROBED

**Steering**: F109 (subscript-reassign FP, loan not killed) and F39 (subscript-write FN, over-kill) are the two known loan-kill bugs at the *subscript* path site. Baseline: scalar `p=&_Mut x; p=&_Mut y; &_Mut x;` is CLEAN (x's loan NLL-killed when p is reassigned — confirmed empirically + dump shows x's loan region `'region_0` = points `["BB1/4"]` only, ending right at the p-reassign). **Distinct targets**: (1) REBORROW `int *_Borrow r=&_Mut *p;` then use both p and r; (2) `_Borrow` whose loan should end when the HOLDER is reassigned through a DIFFERENT path than scalar/subscript (e.g. struct field `s.b=&_Mut y;`); (3) a loan carried across a loop iteration boundary.

**Invariant (intended)**: A loan L on source `x` (created by `b=&_Mut x`) ends when the borrow `b` is no longer LIVE — NLL computes the region of L = {points where b is live}. Reassigning the borrow holder (`p=...`, `s.b=...`, loop-iteration-local `b`) makes b dead from that point → L's region excludes subsequent points → a fresh `&_Mut x` after the reassign is ACCEPTED (not "more than once"). The kill is via the Liveness/region mechanism (not LoansKilledByWriteTo, which kills when the loan's PATH is overwritten — a separate, additional kill).

**Peers**: LoansKilledByWriteTo (:1211, path-prefix overwrite kill); LoansNotInScopeAt (:1250, region kill — the primary NLL mechanism); Liveness::SimulateBlock (:1022, computes live-set feeding region); PopulateInference (:1604, AddLivePoint per live borrow-typed var + EnsureBorrowSource reborrow constraint); EnsureBorrowSource (:2166, supportingPrefixes — adds `r ⊇ p` for reborrow through borrow var).

**Candidates**:
1. (REBORROW, top) `int *_Borrow p = &_Mut x; int *_Borrow r = &_Mut *p;` then `p = &_Mut y;` then use `*r`. r transitively borrows x (via *p). Does reassigning p (the intermediate) correctly end r's transitive loan on x? FP if a fresh `&_Mut x` after is wrongly rejected; FN if a write to x while r live is missed. EnsureBorrowSource adds r⊇p; if p's region dies but r's doesn't reconnect to x, gap. UNPROBED.
2. (STRUCT-FIELD HOLDER) `struct B{int *_Borrow f;} s; s.f=&_Mut x; s.f=&_Mut y; &_Mut x;` — reassign the FIELD that holds the borrow. OverWrites returns Dest=`s.f`; x-loan path=`x` prefixes={x} doesn't contain `s.f` → not killed by path-overwrite (correct for path semantics). NLL must kill it via liveness — but **CONFIRMED-NEW (FP)**: it does NOT. The field-case first x-loan region extends [BB1/5..BB1/16] (to fn-end), not killed at the `s.f=&_Mut y` reassign; the scalar twin's x-loan region is [BB1/4,BB1/5] (ends at the reassign). Root: the loan is driven by whole-struct `s` liveness (live until StorageDead:s at BB1/27); field reassignment does not model that the field's prior referent is dropped. **Asymmetry proven**: scalar twin CLEAN; null-drop (s.f=nullptr) CLEAN; whole-struct reassign (s=(B){...}) CLEAN; DIFFERENT-field reassign (s.g=...) correctly REJECTED. Only same-field-reassign-to-new-borrow FPs. DISTINCT from F109 (subscript path collapse) + F39 (subscript write over-kill FN) — MemberExpr field holder, different root cause (field liveness). **RETRACTED 2026-06-23 — NOT A BUG (confirmed LIMITATION by user)**: whole-variable loan-region liveness is BY DESIGN; field/sub-place liveness is a known NLL-impl limitation, not a fixable defect (cf array all-or-nothing §3.7.3). Filed F112 then retracted + umbrella comment removed (204). DO NOT re-probe loan-region-granularity FPs as bugs. Repro /tmp/explorer_repro.field_holder_loan_not_killed.cbs.
3. (LOOP-CARRIED) a borrow created inside a loop body, used later in the SAME iteration, then the loop back-edges. NLL region should be intra-iteration only (dead at iteration end). If region inference extends the loan across the back-edge (over-approx) → FP rejecting idiomatic per-iteration borrow; if it under-extends → FN missing a cross-iteration conflict. UNPROBED.

## Generics × _Borrow LOAN/LIFETIME tracking through monomorphization (BSCBorrowChecker loan/liveness machinery, NOT ownership setToMoved) — read 2026-06-23, PROBED-SOUND

**Steering (directive)**: the hypothesis that monomorphization may lose borrow-LOAN/LIFETIME tracking inside a generic body/struct — a *distinct* machinery from ownership/moves (which uses `setToMoved`/`IsTrackedType`). The borrow checker's loan tracking is `LoansInScope`/`Liveness`/`FrozenByBorrowOf`/region inference. The question: does routing a borrow through a monomorphized generic (free fn body, generic struct field, generic return) lose the loan the way G12 strips nullability *attribute-sugar*?

**Invariant (intended)**: a `_Borrow` qualifier that survives canonical-type substitution (per `SemaTemplateInstantiateDeclBSC.md` — `_Owned`/`_Borrow` are real qualifiers, NOT AttributedType-sugar, so `getCanonicalType` keeps them) must be visible to the borrow checker's DefUse/ActionExtract visitors on the monomorphized body, so loans are created, frozen, NLL-killed, and dangling-detected identically to the hand-written non-generic body.

**Peers**: G12 (nullability AttributedType-sugar strip via `ConditionalType::desugar`/canonicalization — the *attribute-sugar* dimension, confirmed real FN; `_Owned`/`_Borrow` are NOT this kind of sugar and survive); `bi9znvfyo` sibling explorer (the `_Owned`-through-monomorph analogue — refuted: `_Owned` survives canonical, leak/move/field-leak all fire through the generic; this note is the `_Borrow` loan/lifetime analogue and reaches the same conclusion on the borrow dimension); G14 (rewriter mangle collision — TypePrinter record-name path, NOT borrow-checker loan tracking); F109/F39 (subscript loan-kill, non-generic); F111 (BSCOwnership runOnBlock filter — different analyzer).

**Candidates** (all PROBED-SOUND on fresh bin 34e6f26e, 2026-06-23; each with a non-generic baseline at parity):
1. `void use2<T>(T a,T b){}` + `use2<int*_Borrow>(&_Mut x,&_Mut x)` → REJECTED "cannot borrow x as mutable more than once" (exit 1), IDENTICAL to non-generic `use2(&_Mut x,&_Mut x)`. Two simultaneous mut borrows through a generic conflict-detected. `/tmp/explorer_probe.use_generic.cbs` ; baseline `/tmp/explorer_baseline.use2.cbs`.
2. `int *_Borrow fwdg<T>(T b){return b;}` + caller passes `&_Mut local` of a dying local → REJECTED "local does not live long enough / dropped here while still borrowed" (exit 1), IDENTICAL to non-generic `fwd`. Returned-borrow dangling detection through the generic. `/tmp/explorer_probe.fwdg_dangling.cbs` ; baseline `/tmp/explorer_baseline.dangling_return3.cbs`.
3. `struct Ref<T>{T b;}; struct Ref<int*_Borrow> r={.b=&_Mut x}; int *_Borrow q=&_Mut x;` → REJECTED "cannot borrow x as mutable more than once" (exit 1), IDENTICAL to non-generic `struct Ref`. Field-held loan through a generic struct freezes x. `/tmp/explorer_probe.ref_field_generic.cbs` ; baseline `/tmp/explorer_baseline.ref_field.cbs`.
4. Call-site conflict: caller holds `outer=&_Mut x`, calls `g<int*_Borrow>(&_Mut x)` → REJECTED (x already mut-borrowed), parity with non-generic. `/tmp/explorer_probe.body_write_through_b.cbs` ; baseline `/tmp/explorer_baseline.body_write_through_b.cbs`.
5. NLL write-while-borrowed INSIDE the generic body (`b=&_Mut local; local=2; (void)*b;`) → REJECTED "cannot assign to local because it is borrowed", last-use boundary respected. `/tmp/explorer_probe.nll_inside_body.cbs`.
6. Generic struct `_Borrow` field passed BY VALUE (`sink(struct Ref<int*_Borrow>)`) then concurrent `&_Mut x` → CLEAN (exit 0), parity with non-generic (both clean — correct NLL: the copy + original die before the re-borrow; not a gap). `/tmp/explorer_probe.generic_struct_borrow_copyvalue.cbs` ; baseline `/tmp/explorer_baseline.struct_borrow_copyvalue.cbs`.
7. Generic returns a struct holding a `_Borrow` of a dying local → REJECTED "local dropped here while still borrowed", parity with non-generic. Dangling-borrow-through-generic-return caught. `/tmp/explorer_probe.generic_return_struct_borrow_dangling.cbs` ; baseline `/tmp/explorer_baseline.return_struct_borrow_dangling.cbs`.
8. F109-fold check: generic struct field reassign `r.b=&_Mut y` then `&_Mut x` → CLEAN (exit 0), parity with non-generic (both clean — the field-reassign correctly NLL-kills the loan here; no F109-style gap through the generic field either). `/tmp/explorer_probe.f109_field_through_generic.cbs` ; baseline `/tmp/explorer_baseline.field_reassign.cbs`.

**Conclusion**: GENERICS × _Borrow LOAN/LIFETIME tracking is SATURATED-SOUND on 34e6f26e. Every loan/lifetime dimension — creation, freeze (conflict at call site), NLL last-use kill inside the body, field-held loan through a generic struct, dangling-return detection, by-value-copy loan propagation — is at PARITY with the non-generic baseline. The hypothesis is REFUTED: monomorphization does NOT lose borrow-loan tracking, because `_Borrow` is a real qualifier that survives `getCanonicalType` (only nullability AttributedType-*sugar* is stripped, per G12), so the borrow checker's DefUse/ActionExtract/LoansInScope/Liveness visitors see the borrow on the monomorphized body exactly as if hand-written. This is the `_Borrow`-loan analogue of the `bi9znvfyo` sibling's `_Owned`-through-monomorph refutation — both dimensions (ownership AND borrow-loan) survive monomorphization; only the nullability *attribute-sugar* dimension (G12) is stripped. DISTINCT from G14 (rewriter mangle, not loan tracking), G12 (nullability sugar, not _Borrow qualifier), F109/F39/F111 (non-generic loan/move sites). Recommend next: pivot OFF generics×borrow substitution — the substitution path is exhausted-sound. The residual generics×borrow surface is the F109-style loan-kill gaps routed through generics, but those FOLD into the filed non-generic F109 root (same `UsesArraySubscriptNotation` collapse / field-liveness mechanism, no generic-specific divergence observed).

## DefUse/ActionExtract — NO VisitConditionalOperator (BSCBorrowChecker.cpp) — probe 2026-06-24
**Invariant**: a `?:` whose arms create borrows must contribute BOTH arms' loans (the result may alias
either operand), so a later conflicting borrow/mutation of either referent is caught.
**Gap**: neither DefUse nor ActionExtract overrides VisitConditionalOperator → falls to VisitStmt
(:755) which iterates children and OVERWRITES Sources/Kind/BK per child → if a whole `?:` is one
ActionExtract pass, only the LAST arm's loan survives. Mitigant: clang CFG usually splits `?:` into
per-arm blocks (each arm extracted separately) — reachability depends on that. Peers: VisitBinAssign,
mergeDPVD (F26), F12 (conditional narrowing flow, nullability).
**Candidates**: 1. **conflicting `&_Mut` of an arm referent after `p = c?&_Mut x:&_Mut y` not caught**
(missed x-loan → mutate/double-borrow FN) UNPROBED ⭐. 2. arm referent freed/moved while p live. 3. nested `?:`.

## return borrow-to-local from fn with borrow param (region inference return) — probe 2026-06-24
**Invariant**: a returned `_Borrow` value's region must be tied to a parameter's region; returning
`&_Mut local` (region ends at fn exit) cannot satisfy the return-type region → must be rejected.
**Peers**: err_typecheck_borrow_func (signature gate), RegionInference, "does not live long enough".
**Candidates**: 1. **`int *_Borrow f(int *_Borrow p){ int x; return &_Mut x; }` — dangling local borrow
escapes: rejected (sound) vs accepted (FN, UAF on caller deref)** UNPROBED ⭐. 2. return &_Mut *p (reborrow param, OK). 3. return a borrow to a local through a struct field.

## ActionExtract::VisitInitListExpr borrow-field init (BSCBorrowChecker.cpp:619-647) — read 2026-06-24
**Invariant**: for a tracked struct, iterate fields() in lockstep with getInit(Index), extracting an
Assign action per borrow field so its loan is recorded. Field/init index alignment must hold (designated/
partial inits rely on the SEMANTIC init-list form filling all fields).
**Peers**: VisitMemberExpr, LoansInScope, union branch (single active member vs all fields()).
**Candidates**: 1. **designated struct init `{.b=&_Mut x}` — s.b loan tracked (conflict detected) vs index
misalign loses loan (FN)** UNPROBED ⭐. 2. union borrow-field init (fields() iterates all, 1 init). 3. partial init.

## mutate owner directly while mutably borrowed — probe 2026-06-24
**Invariant**: while `b = &_Mut x` is LIVE, a direct write `x = 2` to the borrowed owner must conflict
(exclusive mutable access). NLL: only if b is used after the write.
**Peers**: F11/F21 (mutate THROUGH borrow after owner freed — opposite direction), LoansInScope, liveness.
**Candidates**: 1. **`b=&_Mut x; x=2; use(b);` — direct owner-write while b live: rejected (sound) vs accepted (FN)** UNPROBED ⭐.
2. read x (not write) while &_Mut borrowed. 3. write x while &_Const borrowed.

## ActionExtract::VisitCStyleCastExpr (BSCBorrowChecker.cpp:507-~540) — read 2026-06-25
INVARIANT: extracts the borrow ACTION from a C-style cast on the RHS — if the cast type is borrow-qualified and
Sources[0] is non-null & not-already-borrow, set Kind=Borrow with BorrowKind=Shared(const)/Mut, region from the
cast; skips CK_NullToPointer sub-casts; suppresses AddDeref for `(T*_Borrow)&x` AddrOf subexprs.
PEERS: DefUse::VisitCStyleCastExpr, ActionExtract::VisitCallExpr/VisitMemberExpr; IsSafePointerConversion (G15).
CANDIDATES (probed — all UNREACHABLE in _Safe / sound):
1. dangling borrow via cast-of-&local outliving scope — UNREACHABLE: `&local` is "'&' operator forbidden in the
   safe zone"; borrow-return-of-local hits "return type not allowed _Borrow w/o borrow param" (F114-area). So the
   cast-borrow path never sees `&local`; it operates only on already-valid borrows.
2. empty Sources → borrow action not recorded (:517 guard) — only reachable if the subexpr yields no source; the
   reachable borrow-cast inputs (re-borrow, const-add, void-erase) all yield a source. Not a hole.
3. re-borrow via cast (Sources[0] already borrow → Kind not set Borrow, :524) — correct: the original loan covers
   the derived re-borrow (NLL). The const/void-erase conversion FP in this family is already filed = G15.
SOUND for reachable inputs. The borrow-cast loan tracking operates on valid borrows; dangling-of-local is blocked
upstream by the _Safe & / return-borrow rules.


## InferenceContext::Solve + DFS::Copy (BSCBorrowChecker.cpp:952-1030) — read 2026-06-25 (FreeRV-cap candidate RESOLVED)
INVARIANT: fixpoint constraint solver — iterate constraints; DFS::Copy propagates Sup region points into Sub
(reachable via SuccessorPoints from constraint.point); if a copy GROWS a CAPPED (free) region → llvm_unreachable
"Free region should not grow anymore!" (:972, also :943).
CANDIDATE (BSCBorrowChecker.md:250 FreeRV-cap crash) — RESOLVED reasoned-safe + PROBE-CONFIRMED 2026-06-25:
PreprocessForParamAndReturn pre-populates FreeRV with all blocks' points (1..size) + EndPoint then CapVar (:2227-
2237); DFS::Copy's SuccessorPoints (:1021) only emits points in [1,size]+EndPoint, all already in the capped set →
AddPoint returns false → no growth → unreachable never fires. Probed deep borrow-param region flow (nested loops +
reborrows + branches, /tmp/claude-998/deepbr.cbs + earlier br1/br2/br3) on the ASSERT build → no crash/assert
(rc=1, borrow errors but no llvm_unreachable). FreeRV-cap surface CLOSED for reachable _Borrow scenarios. SOUND.


## FindLoansThatIntersect / FrozenByBorrowOf / structurallyEquals (BSCBorrowChecker.cpp:1501-1598) — read 2026-06-25
INVARIANT: loan-intersection + freeze for the borrow conflict check.
- FindLoansThatIntersect (:1542): a path access intersects a loan iff the path's prefixes structurally-equal the
  loan path (access a.b.c hits loans of a.b.c / a.b / a) OR the loan's supportingPrefixes equal the path (access
  a.b.c hits a loan of a.b.c.d). Sound coverage.
- FrozenByBorrowOf (:1571): walks up the borrowed path collecting paths whose write would invalidate it. KEY
  correctness (:1585): if base is a NON-OWNED pointer, borrowing `*r` then re-pointing r does NOT invalidate the
  borrowed memory → STOP (don't freeze r); for an OWNED pointer, re-assigning p drops/replaces `*p` → continue
  (freeze p). This owned-vs-non-owned distinction is correct.
CANDIDATES (sound, no new): (1) owned-vs-non-owned freeze (:1585) — correct (re-point borrow≠invalidate; re-assign
  owned=invalidate); (2) prefix/supporting-prefix intersection — correct coverage; (3) structurallyEquals path
  matching — deep equality, no observed defect. Borrow-conflict loan machinery SOUND. Borrow-checker frontier
  candidates (Solve/FreeRV-cap, VisitCStyleCast, FindLoans*/Freeze) all read + SOUND.

## CheckBorrows / CheckMove (BSCBorrowChecker.cpp:1396-1460) — read 2026-06-25 (conflict-report matrix)
INVARIANT: report borrow conflicts. CheckBorrows: Shallow→FindLoansThatFreeze / Deep→FindLoansThatIntersect, then
per conflicting loan × accessMode: Read+Shared=OK, Read+Mut→ForImmutWhenMut/ForRead; Write+Shallow→ForWrite,
Write+Deep+Mut→ForMultiMut (=F39 "borrow arr[] mutable more than once"), Write+Deep+Shared→ForMutWhenImmut.
CheckMove: any intersecting loan → ForMove (stricter than write — can't move a borrowed path even when write is OK).
CANDIDATES (no new): the Read/Write × Shared/Mut × Shallow/Deep matrix is COMPLETE; F21/F24/F39/F42 are upstream
loan-LIVENESS/tracking gaps (when a loan is/ isn't in scope) feeding this matrix, not matrix defects. CheckBorrows/
CheckMove SOUND. Borrow-checker now documented: DefUse/ActionExtract visitors + Solve/FreeRV + FindLoansThat*/Freeze
+ CheckBorrows/CheckMove conflict matrix + VisitCStyleCast. Defects=F09/F11/F21/F24/F39/F42/F109 (loan-tracking/CFG).

## ActionExtract/DefUse wrapper-recursion (BSCBorrowChecker.cpp:65/217, StmtVisitor) — adjacent-fix re-probe 2026-06-26 @411b4118
INVARIANT: the borrow-checker visitors must reach every borrow-creation/move ACTION nested inside expression
wrappers so loans + moves are tracked. Both DefUse and ActionExtract are clang::StmtVisitor (NO auto-recursion);
they manually recurse per opcode: VisitBinaryOperator handles BO_Mul..Shr / BO_And..LOr / Add/Sub / LT..NE /
compound-assign (:457-478); VisitBinComma visits LHS+RHS (:482, the 6daa7de F11/F93 fix); NO VisitConditionalOperator.
PEERS: F11/F93 (comma launder, FIXED 6daa7de), F118 (temp-leak &&/|| gap — structurally different: discarded-expr).
CANDIDATES:
1. (ternary launder, PROBED-SOUND) borrow `int*_Borrow b = c ? &_Mut *o : &_Mut *o;` then free o → CAUGHT
   ("cannot move out of o because it is borrowed", same as comma). Loan is bound to b + tracked from the decl-init
   regardless of the ternary wrapper; no VisitConditionalOperator needed for the named-borrow path. NOT a gap.
2. (&&/|| operand move, likely sound) ActionExtract DOES visit BO_LAnd..LOr operands (:459) — unlike temp-leak's
   BO_Comma-only arm — so a move in a &&/|| operand is extracted. UNPROBED but source says covered.
3. (StmtExpr / other unhandled wrappers) StmtVisitor skips unhandled kinds; StmtExpr is OOS. No in-scope gap.

## NLL loan liveness across loop back-edges (BSCBorrowChecker Liveness/RegionCheck, 2026-06-26)
INVARIANT: a loan (borrow) is live from creation to its LAST use along ALL CFG paths incl. loop back-edges; a
conflicting action (move/free/mutate of the borrowed value) while the loan is live must be rejected. A loop where the
borrow is re-used at the top after a conflicting action at the bottom requires the back-edge to extend liveness.
PEERS: F21/F25/F79 (move-through-borrow FNs, filed), bc_tern (ternary borrow CAUGHT), LoansInScope, Liveness.
CANDIDATES:
1. (loop back-edge liveness, UNPROBED top) `b=&_Mut *o; for(i){ *b=i; if(i==0) free(o); }` — b used iter-1 after o
   freed iter-0; if liveness omits the back-edge, free-while-borrowed MISSED → UAF FN. Probe + valgrind.
2. (borrow live into loop-exit) borrow used only after the loop but created inside → exit-edge liveness.
3. (nested-loop carried) borrow from outer loop used in inner after inner-body conflict.

## Loan propagation through reborrow chains (RegionInference, 2026-06-26)
INVARIANT: `b2 = b1` (b1 borrows o) must propagate b1's loan to b2 so that a conflicting action on o while b2 is
live is rejected (the outlives constraint b2⊇b1⊇loan(o) must transit the assignment).
PEERS: nll_lin (direct b=&_Mut*o, free-while-borrowed CAUGHT), F21/F25/F79 (move-through-borrow FNs), LoansInScope.
CANDIDATES:
1. (reborrow-chain loan propagation, UNPROBED top) `b1=&_Mut*o; b2=b1; free(o); *b2=5` — if the assignment b2=b1
   loses b1's loan, free-while-b2-borrowed is MISSED → UAF. Probe + valgrind.
2. (reborrow then b1 dies, b2 live) does b2 keep the loan after b1's last use?
3. (struct-field reborrow) store b1 in a struct field, reborrow from it.

## F119 PRECISE ROOT — ActionExtract::VisitCStyleCastExpr (BSCBorrowChecker.cpp:524-543)
For a borrow-qualified CStyleCast (`(int*_Borrow)X`): Visits subexpr (:531), then at :541 sets Kind=Action::Borrow
ONLY `if (!Sources[0]->ty.isBorrowQualified())` — a guard intended to skip RE-borrows of an already-borrow source.
BUG: when X is itself a FRESH borrow-creation `&_Mut *o`/`&_Const *o`, Sources[0]->ty IS borrow-qualified, so the
guard suppresses recording the new Borrow action → no loan (b⊇o) → free-while-borrowed not flagged → F119 UAF.
The DIRECT `b=&_Mut *o` records the loan via the AddrOf path; the cast diverts into this guard. FIX: at :541, also
treat the case where the cast's SUBEXPR is a fresh borrow-creation (UO_AddrOf of `&_Mut`/`&_Const`) as a Borrow,
not a reborrow — i.e., distinguish "cast of a borrow-CREATION" from "cast of an existing borrow VALUE".

## DefUse def/use tracking (BSCBorrowChecker.cpp:65-205, StmtVisitor) — 2026-06-26 Mode-1
INVARIANT: computes def/use sets for borrow LIVENESS. VisitUnaryDeref (:180) `*x`=Use of x; VisitUnaryOperator
(:185) inc/dec = Def+Use; VisitArraySubscript reads base even on LHS (:198); VisitBinAssign LHS=Def RHS=Use;
VisitBinComma visits both. PEERS: ActionExtract (loan/action, F119 lives there), the ownership use-of-moved (separate).
CANDIDATES: 1. deref=Use is correct (liveness sees `*o`); the round-4 discarded-`*o;`-after-move gap is the OWNERSHIP
analysis (TransferFunctions), NOT DefUse — distinct analyses. 2. inc/dec Def+Use ordering — sound. 3. no obvious gap.

## DefUse borrow def/use visitor family (BSCBorrowChecker.cpp:95-130, 2026-06-27 Mode-1)
INVARIANT: complete opcode coverage for borrow def/use. VisitBinaryOperator (:95): arithmetic/bitwise/logical/comparison
(BO_Mul..BO_Shr, BO_And..BO_LOr, BO_LT..BO_NE) → Action=Use, visit both; compound-assign (BO_MulAssign..BO_OrAssign) →
Def(LHS)+Use(LHS,RHS). Plain BO_Assign is NOT in VisitBinaryOperator's ranges — clang's StmtVisitor auto-dispatches it to
VisitBinAssign (:118): Def(LHS, isAssign=true)+Use(RHS). VisitBinComma (comma), VisitCallExpr (:126, args→Use). PROBED-SOUND:
checked the apparent BO_Assign gap — VisitBinAssign covers it. All binary opcodes routed. (ActionExtract has its own
VisitBinAssign :491.) No coverage hole here.

## DefUse::VisitArraySubscriptExpr (:196) + VisitMemberExpr — borrow def/use leaf visitors (2026-06-27 Mode-1)
ArraySubscriptExpr: in Use/Def context, base pointer read as Use even on assignment LHS (`p[i]=x` reads p) + idx as Use;
else VisitStmt. MemberExpr: Def+isAssign → downgraded to Use (a field assign `x.a=...` READS x, doesn't reassign the whole
x); Use → visit base. Both SOUND. DEFUSE FAMILY NOW FULLY READ: VisitBinaryOperator/VisitBinAssign/VisitBinComma/VisitCallExpr
/VisitArraySubscriptExpr/VisitMemberExpr — complete, no coverage hole. The borrow def/use surface is sound; the known borrow
bug (F119) is in ActionExtract::VisitCStyleCastExpr (the loan-record path), NOT the DefUse pass.

## ActionExtract borrow-create handlers (loan-record mechanism, F119 neighborhood) (2026-06-27 Mode-1)
VisitUnaryAddrMut (:803) / VisitUnaryAddrMutDeref (:810) / VisitUnaryAddrConst (:779) / VisitUnaryAddrConstDeref (:786):
each sets RNR=region name, BK=Mut/Const, **Kind=Action::Borrow** (this is what RECORDS THE LOAN), then visits subexpr;
the Deref variants additionally build a "*" Path for Sources[0]. So the loan is recorded iff Kind==Action::Borrow is set.
F119 ROOT IN CONTEXT: VisitCStyleCastExpr (:524) guards `if (!Sources[0]->ty.isBorrowQualified()) Kind=Action::Borrow;`
— when the cast wraps an ALREADY-borrow-qualified value (the &_Mut* result), the guard SKIPS setting Kind=Borrow → loan
NOT recorded → free-while-borrowed unflagged. The fix (IgnoreParenCasts before the guard, or always re-derive Kind from the
underlying borrow-create) restores the loan. ActionExtract family now read: ArraySubscript/Binary/BinComma/BinAssign/Call/
CStyleCast/DeclRef/DeclStmt/InitList/Member/Return/Stmt/UnaryAddr{Mut,Const}{,Deref}/UnaryDeref — F119 is the sole defect.

## BorrowCheck::CheckBorrows (BSCBorrowChecker.cpp:1413) — core loan-conflict / exclusivity matrix (2026-06-27)
INVARIANT: for loans affecting `path` (Shallow→FindLoansThatFreeze, Deep→FindLoansThatIntersect), enforce: Read vs Shared=OK,
Read vs Mut=ForRead/ForImmutWhenMut; Write(shallow)=ForWrite, Write(deep) vs Mut=ForMultiMut, vs Shared=ForMutWhenImmut.
Reports first conflict + returns. PEERS: CheckAction(:1335) dispatch, CheckMove/CheckMutBorrow/CheckRead/CheckShallowWrite/
CheckStorageDead, LoansInScope::Compute (loans-in-scope dataflow), Liveness::Compute. The exclusivity matrix is complete;
soundness depends on correct loans-in-scope (F119 = upstream loan-record gap makes loans empty → CheckBorrows finds no conflict).
CANDIDATES: 1. write-through-owner while shared/const borrow live → ForMutWhenImmut? UNPROBED→probing. 2. Shallow/Deep depth
selection edge. 3. matrix complete given loans (sound).

## LoansInScope::SimulateBlock + Compute (BSCBorrowChecker.cpp:1246/1298) — NLL loan dataflow (2026-06-27)
INVARIANT: loans-in-scope per point = pred-union at entry; per element: KILL loans whose region excludes `point`
(LoansNotInScopeAt, NLL liveness), GEN loans created at point (loansByPoint), KILL loans whose borrowed path is OVERWRITTEN
(LoansKilledByWriteTo per action->OverWrites()). Fixed-point in Compute. Feeds CheckBorrows (the conflict matrix). PEERS:
InferenceContext/AddLivePoint (region), Liveness::Compute, CheckBorrows. CANDIDATES: 1. (kill-on-overwrite granularity) does
writing a SIBLING field (s.a) over-kill a loan of s.b → 2nd &_Mut s.b not flagged as double-borrow? UNPROBED→probing.
2. (LoansNotInScopeAt region too short → premature kill → conflict missed) region-inference FN. 3. pred-union sound (conservative).

## InferenceContext region inference (BSCBorrowChecker.cpp:953 AddLivePoint / :970 Solve / :1007 DFS::Copy) (2026-06-27)
INVARIANT: computes each borrow's REGION (NLL lifetime) by fixed-point over outlives-constraints (sub ⊇ sup from a point, via
DFS::Copy); "free/capped" regions (param/stack lifetimes) must NOT grow (llvm_unreachable guard — but asserts OFF in canonical
build → a constraint that would grow a capped region is a lifetime-FN risk). Underpins LoansNotInScopeAt (region too short →
premature kill) + return-escape/lifetime checks. CANDIDATES: 1. return-escape: returning a borrow of a LOCAL (capped region)
must be rejected ("does not live long enough"). 2. ternary/wrapper-wrapped return-escape — does the region propagate through?
3. capped-region-grows-in-release (asserts off) → lifetime FN. UNPROBED→probing 1+2.

## BorrowCheck::Check* access family (BSCBorrowChecker.cpp:1474-1503) — access-to-loan dispatch (2026-06-27)
INVARIANT: each access type → loan check: CheckMove (any intersecting loan → ForMove, strictest), CheckMutBorrow=CheckBorrows
(Deep,Write), CheckRead=CheckBorrows(Deep,Read), CheckShallowWrite=CheckBorrows(Shallow,Write) for `x=...` overwrite,
CheckStorageDead (FindLoansThatFreeze → ForStorageDead; freeing var illegal if INTERIOR data borrowed, but `*var` borrowed OK
since free kills the loan). All route to CheckBorrows (matrix sound) or FindLoansThatFreeze/Intersect. Complete dispatch.
CANDIDATES: 1. overwrite owned `o=mk()` without consuming old o → leak of old value (ownership, not borrow) — caught? 2.
CheckStorageDead `*var`-borrowed-ok edge. 3. CheckMove strictness sound. UNPROBED→probing 1.

## ActionExtract::VisitCallExpr (:513) + VisitCStyleCastExpr (:524, F119 root) (2026-06-27)
VisitCallExpr: Noop→Use, else if Dest→Init; visits each arg. Does NOT tie return-borrow→arg here — the inter-procedural
lifetime (return _Borrow ⊇ param _Borrow) is via the signature + region inference. VisitCStyleCastExpr: confirms F119 root —
`if (!Sources[0]->ty.isBorrowQualified()) Kind=Action::Borrow;` SKIPS loan when cast source already borrow-qualified
(`(int*_Borrow)&_Mut*o`). Also: CK_NullToPointer cast skipped; AddDeref logic; UO_AddrOf sub → no deref. CANDIDATES: 1.
inter-procedural: borrow returned from a fn, source freed → UAF caught via signature lifetime? UNPROBED→probing. 2. F119
(cast loan-skip, filed). 3. AddDeref path-shape edge.

## FindLoansThatFreeze (BSCBorrowChecker.cpp:1518) — freeze-set for write/storage-dead (2026-06-27)
INVARIANT: a loan "freezes" path P if FrozenByBorrowOf(loan.path) structurally-equals P (borrowing a.b freezes a, a.b) OR a
prefix of P structurally-equals the loan.path (borrowing a.b prevents writes to a.b.c). Used by CheckShallowWrite + CheckStorageDead.
PEERS: FindLoansThatIntersect (deeper, for read/move), CheckStorageDead. CANDIDATES: 1. storage-dead escape — inner-block
local borrow used after the block (x's scope ends while borrowed) → ForStorageDead caught? UNPROBED→probing. 2. FrozenByBorrowOf
prefix-logic edge. 3. structurallyEquals path-compare sound.

## FindLoansThatIntersect (:1559) + FrozenByBorrowOf (:1589) (2026-06-27)
FindLoansThatIntersect: accessing a.b.c intersects loans of prefixes (a.b.c/a.b/a) AND of extensions (a.b.c.d) via
structurallyEquals — for read/move conflicts (deeper than freeze). FrozenByBorrowOf: walks path upward collecting frozen
paths; STOPS at `*r` when base r is a non-owned pointer (writing r doesn't affect *r's memory — sound). CANDIDATES: 1.
array-index granularity: does `arr[i]`/`arr[j]` (dynamic) structurallyEqual (index-agnostic→conservative conflict caught) or
differ (index-specific→aliasing FN if i==j)? UNPROBED→probing. 2. supportingPrefix extension-intersect sound. 3. FrozenByBorrowOf *r-stop sound.

## VisitUnaryExprOrTypeTraitExpr (sizeof/alignof) — both checkers no-op — PROBED-SOUND 2026-06-29
**Invariant**: the operand of `sizeof`/`alignof`/`_Alignof` is UNEVALUATED, so a moved/borrowed pointer inside
`sizeof(*p)` must NOT count as a use. Both `TransferFunctions::VisitUnaryExprOrTypeTraitExpr` (BSCOwnership.cpp:2269)
and `ActionExtract::VisitUnaryExprOrTypeTraitExpr` (BSCBorrowChecker.cpp:872) are `{ return; }` (no-op) — they do
NOT recurse into the operand, correctly skipping the unevaluated expr. **Peers**: VisitCStyleCastExpr, VisitUnaryDeref.
**Probe** `int *_Owned p=mk(); consume(p); return sizeof(*p);` → compile rc=0 (correctly NOT use-after-move),
valgrind 0 errors. No FP (operand not treated as use), no FN (operand genuinely never evaluated → no runtime UAF).
**Candidates**: 1. VLA `sizeof(int[n])` where `n` borrows — VLAs likely restricted in _Safe, not pursued. SOUND.

## RegionCheck::EnsureBorrowSource (BSCBorrowChecker.cpp:2183) — reborrow outlives-constraint — read 2026-06-29
**Invariant**: for a borrow whose SOURCE path passes through a `_Borrow`-qualified prefix (a reborrow),
add `AddOutLives(BorrowRV, RefRV, SuccPoint)` so the new borrow's region cannot outlive the base borrow's
region — i.e. a reborrow `&_Mut *p` may not outlive `p`'s referent. Walks `SourcePath->supportingPrefixes()`.
**Switch is EXHAUSTIVE**: `Path::PathType` has only {Var, Extension} (BSCBorrowChecker.h:119), both handled —
no C2 opcode-switch hole. `Var` → return (no constraint); `Extension` → if `base->ty.isBorrowQualified() &&
base->D != nullptr`, add outlives.
**Peers**: RelateRegions, PopulateInference, LiveRegions, FindLoansThatIntersect, AddLivePoint.
**Probe** reborrow that outlives source scope (`{int y; p=&_Mut y; outer=&_Mut *p;} use(outer);`) → correctly
REJECTED "`y` does not live long enough" (rc=1). Basic reborrow lifetime SOUND.
**Candidates**:
1. (C2 switch hole) — REJECTED, switch exhaustive (2/2 PathType handled).
2. (null-Decl borrow base, UNPROBED) — the `Extension` case skips the outlives-constraint when
   `base->D == nullptr` (a `_Borrow`-qualified base with no Decl: temporary / call-result / array-elem reborrow).
   If such a reborrow can be formed in valid `_Safe` and outlive its source → use-after-free FN. Hard to construct
   (reborrows restricted; paths root at Var-with-Decl); borrow checker is exhaustively-audited (F119 sole defect).
   LOW priority, focused future session.

## InferenceContext::Solve (BSCBorrowChecker.cpp:969) — NLL region-inference fixpoint — read 2026-06-29
**Invariant**: iterate-to-fixpoint over outlives `constraints`; for each, `dfs.Copy(Sup, SubDef.value, point)` grows the
sub-region to include points reachable from the constraint start s.t. every borrow's region covers all its uses; a
`capped` region (free/abstract/param/static) must NOT grow — if it would, `llvm_unreachable("Free region should not
grow anymore!")`. The capped guard encodes "a borrow may not outlive a free/param region" (escape).
**Peers**: DFS::Copy, PopulateInference (:1621), RelateRegions (:2209), EnsureBorrowSource, LiveRegions, Liveness::Compute.
**Candidates**:
1. (capped-region-grow → `llvm_unreachable`, UNPROBED — needs ASSERT build) if a valid/invalid BSC program can make a
   CAPPED region grow at :Solve, it hits `llvm_unreachable` → assert-build crash / asserts-off UB. The escape SHOULD be
   caught earlier (CheckBorrows / RelateRegions reject before Solve); if not, crash. Canonical build has asserts OFF so
   UB not a clean crash — re-test on an assert build (see reference_assert_builds). RANK: MEDIUM (crash-class, hard to construct).
2. (escape via struct-field store, PROBE) store `&_Mut local` into a longer-lived `_Borrow` field → inference must reject.
3. (fixpoint termination / capped interaction) a constraint cycle among capped+free regions — DFS.Copy convergence.

## Liveness::Compute / SimulateBlock (BSCBorrowChecker.cpp:1060) — backward liveness fixpoint — read 2026-06-29
**Invariant**: `liveness[B]` = vars live at B's entry, via backward dataflow: successors' live set flows to block exit,
then walking statements in REVERSE, `DefUse` uses GEN (add to live) and defs KILL (remove). Fixpoint to convergence.
A borrow's region must cover where its holder is live; ending liveness too early ends the loan early → conflicting
op wrongly allowed (FN). **Peers**: DefUse (per-stmt def/use), VisitLifetimeEnds (storage-dead kill), LoansInScope::Compute,
PopulateInference, InferenceContext::Solve.
**Candidates**:
1. (DefUse misses a use → liveness ends early → loan ends early → conflicting borrow/mutation FN) — same handler-bug
   class as F119 (cast) / F11 (comma); the default DefUse::VisitStmt recurses so coverage gaps don't apply, only handler bugs.
2. (loop back-edge liveness, PROBE) borrow used AFTER a loop must stay live ACROSS it → in-loop conflict caught.
3. (LifetimeEnds over/under-kill) a var's storage-dead point mis-killing liveness.

## LoansInScope::Compute / Walk (BSCBorrowChecker.cpp:1298) — active-loan dataflow — read 2026-06-29
**Invariant**: `loansInScopeAfterBlock[B]` = loans active after block B (forward fixpoint over reverse_nodes); a loan is
in scope from its creation until its inference region ends. `Walk` enumerates active loans per point → `CheckAction`
flags a conflicting access (write to a borrowed place / second mut borrow) while a loan is in scope. **Peers**:
InferenceContext::Solve (loan regions), Liveness::Compute, CheckAction, FindLoansThatIntersect/Freeze, BorrowCheck.
**Candidates**:
1. (loan region wrong → in-scope set wrong) too-small → loan killed early → conflict missed (FN, = F39 subscript over-kill class); too-large → FP. Derived from inference (PROBED-SOUND) + liveness (PROBED-SOUND).
2. (branch-sensitivity, PROBE) loans in disjoint if/else branches must NOT conflict; must end at the join.
3. (loan indexing in Walk enumerate) wrong loan reported — index/enumerate mismatch.

## DefUse::VisitBinComma + ActionExtract::VisitBinComma (BSCBorrowChecker.cpp:112-116, 482-489) — PROBED-SOUND @34883aa1 (Chain B reopened, fix 6daa7debe469 closing F11)

> All 3 candidates below PROBED-SOUND 2026-06-29 (assignment-in-comma binds loan to dest; call-arg/two-borrow conflict caught; move-in-comma tracked by ownership). Also re-walked the _Borrow _ArrayElem fix (908ddef2d440/53bc93dd9672): structurallyEquals path-compare unifies `*p`/`p[]` notations (soundness improvement over to_string), arithmetic reborrow carries lifetime, increment-as-Use arm stricter-not-weaker. See _probed.md 2026-06-29. Re-SATURATE Chain B borrow-side.

**Invariant**: a comma `(L, R)` must (DefUse) count BOTH operands as uses for NLL liveness, and (ActionExtract) extract a borrow/move from either operand so the borrowee is frozen — the comma's VALUE is its RHS, so a borrow that initializes a dest must come from R, while a borrow USED in L must keep its referent frozen up to that use.

**Peers**: ActionExtract::VisitBinAssign (491-511, sets Dest/RNL then op=RHS), VisitBinaryOperator (456-480, opcode-range dispatch — comma was the fallthrough hole), ownership BO_Comma special-case (BSCOwnership.cpp:2177, already handled), Prologue TransformBinaryOperator (SemaDeclBSC.cpp:970 hoist / 1464 restore).

**Structural observation**: ActionExtract::VisitBinComma (482) extracts the LHS via a FRESH `ActionExtract(BO->getLHS(), nullptr, ...)` — the outer Dest/RNL/op state is NOT propagated into the LHS sub-extract; only the RHS is visited in the outer context with `op = RHS`. So a borrow created in the comma's LHS gets its loan bound to the discarded temp (correct per the regression test `lhs_borrowee_not_frozen`), NOT to the outer dest. DefUse::VisitBinComma hard-sets `Action = Use` for both operands.

**Candidates**:
1. **Comma RHS that is itself an ASSIGNMENT creating a borrow** — `int *_Borrow m; int t = (m = &_Mut x, 0);` — DefUse comma sets Action=Use then visits RHS `m = &_Mut x`; VisitBinAssign overrides to Def. Does the loan on m survive, and is m's later use frozen? Symmetry vs the tested `m = (0, &_Mut x)` form (assignment OUTSIDE the comma). reachability: the assignment-INSIDE-comma form is not in the regression tests.
2. **Owned MOVE buried in a comma operand** — `consume((x, mk_owned()))` or `int *_Owned o = (consume(p), mk())` — ActionExtract::VisitBinComma fresh-extracts LHS with no Dest; does an owned consume in the LHS get its move recorded? This is ownership (BSCOwnership BO_Comma is separate), but the borrow-checker liveness for an owned-borrow interaction could diverge. composition: comma×owned-move.
3. **Comma as a function-call ARGUMENT that creates a borrow** — `use((0, &_Mut x))` — the comma is nested inside a CallExpr arg, not a decl-init. ActionExtract::VisitCallExpr visits args with op=RHS; the arg is a comma → VisitBinComma. Does the borrow in the comma RHS freeze x for the call's duration / the loan get attached? reachability: call-arg position untested (regression tests all use decl-init).

## RelateRegions / PreprocessForParamAndReturn / IsTrackedTypeImpl (BSCBorrowChecker.cpp:25-52,2209-2235) — read 2026-06-29
**Invariant**: PreprocessForParamAndReturn ties all borrow-tracked params to a shared free region when the RETURN type
is tracked, so a returned borrow can't outlive a param's referent. Gated on `IsTrackedType(return)`. **IsTrackedTypeImpl
IS RECURSIVE** (:38-48 iterates struct fields → returns true if ANY field tracked; :28 owned-ptr recurses pointee;
:31 array recurses element; :36 borrow-qualified → true). So struct-with-borrow-field returns ARE detected (unlike the
ownership/Sema outer-only IsTrackedType = F79/F80/F64). **Peers**: EnsureBorrowSource, InferenceContext::Solve, AddOutLives.
**Candidates**: 1. (IsTrackedType-misses-borrow-field, REFUTED — recursive). 2. param-region over-share (all params one region → FP? conservative, sound). 3. visited-set cycle (:40-42 returns false on a recursive struct → could miss a borrow field behind a cycle, UNPROBED edge).

## CheckStorageDead (BSCBorrowChecker.cpp:1503) — storage-end dangling-borrow check — read 2026-06-29
**Invariant**: at a path's storage-dead point (variable scope-exit), `FindLoansThatFreeze(path)` returns active
borrows of it; each → ForStorageDead diag (the borrow would dangle). **Peers**: FindLoansThatFreeze, CheckStorageDead's
caller (storage-dead CFG element), LoansInScope. **Candidates**:
1. (FindLoansThatFreeze subscript-path miss, F109-class, PROBE) a borrow of an ARRAY ELEMENT `&_Mut arr[i]` escaping arr's scope → if the subscript path isn't matched by FindLoansThatFreeze → dangling not caught (FN).
2. field-path storage-dead (borrow of `s.f` when s dies).
3. storage-dead firing completeness (all scope exits).

## CheckBorrows / CheckShallowWrite vs CheckMutBorrow (BSCBorrowChecker.cpp:1413,1483-1497) — read 2026-06-29
**Invariant**: borrow-conflict check parameterized by Depth (Shallow=FindLoansThatFreeze exact-path / Deep=FindLoansThatIntersect path+subpaths) × Mode (Read: Shared ok, Mut→ForRead/ForImmutWhenMut; Write: Shallow→ForWrite any freeze, Deep→ForMultiMut on Mut). CheckShallowWrite=Shallow/Write, CheckRead=Deep/Read, CheckMutBorrow=Deep/Write. **Peers**: FindLoansThatFreeze, FindLoansThatIntersect, CheckAction. **Candidates**:
1. (Shallow-write misses sub-path loan, PROBE) whole-struct write `s = {...}` while a FIELD `s.f` is mutably borrowed — if Shallow's FindLoansThatFreeze(s) misses the s.f sub-path loan → exclusivity violation FN.
2. shallow-vs-deep classification of an op (a write classified Shallow that should be Deep).
3. Read-mode Mut-loan ForImmutWhenMut vs ForRead branch (IsBorrow flag).

## FindLoansThatIntersect (BSCBorrowChecker.cpp:1559) — deep path-intersection loan finder — read 2026-06-29
**Invariant**: an access path intersects a loan iff one is a structural prefix of the other (bidirectional): (1) access's prefixes vs loan-path (accessing a.b.c hits loans of a.b.c/a.b/a); (2) loan-path's supportingPrefixes vs access (accessing a.b.c hits a loan of a.b.c.d). Uses `structurallyEquals` (F109-fix, ignores subscript notation). **Peers**: FindLoansThatFreeze (exact, shallow), CheckBorrows, Path::prefixes/supportingPrefixes. **Candidates**:
1. (structurallyEquals residual gap — Chain B confirmed sound for *p/p[]).
2. (whole-borrow vs field-write intersection, PROBE) borrow whole `s`, write field `s.a` → s.a's prefix s intersects loan of s → must conflict.
3. prefix-computation depth (deref levels in prefixes()).

## BorrowCheck::CheckAction (BSCBorrowChecker.cpp:1335) — borrow-action conflict dispatcher
- **Invariant**: each lowered Action (Assign/Borrow/Init/...) must be checked against live loans so a write/move/borrow that conflicts with an outstanding borrow is rejected.
- **Peers**: CheckShallowWrite, CheckRead, CheckMutBorrow, CheckMove; ProcessDeref/RecursiveForFields (build DerefSources, F124-adjacent array-blind).
- **Candidates**: (1) **PROBED-folded-F64/F80**: line 1370 `isOwnedQualified()||isMoveSemanticType()` chooses CheckMove-vs-CheckRead for Init sources — shallow isMoveSemanticType → pointer-to-owned source (`int*_Owned*`) takes CheckRead (not move-tracked) = F64-family. (2) **PROBED-SOUND**: `CheckShallowWrite(Dest)` correctly catches a deep write-while-borrowed (`&_Mut o.inner.x` then `o.inner.x=99` REJECTED "cannot assign ... because it is borrowed") AND is field-PRECISE (sibling `o.inner.y=99` while `.x` borrowed ALLOWED rc=0; unrelated var write allowed). Path-based loan tracking sound+precise at field granularity. (3) **UNPROBED**: DerefSources branch `isConstBorrow()? CheckRead : CheckMutBorrow` — a deref source whose base is neither const nor mut borrow defaults to CheckMutBorrow (mut); is an owned-pointer-deref source mis-checked?

## InferenceContext::Solve + DFS::Copy (BSCBorrowChecker.cpp:969/1007) — NLL region-constraint solver
- **Invariant**: fixpoint over outlives constraints — for each `constraint` (sup,sub,point), `DFS.Copy` propagates Sup's CFG-reachable points into Sub starting at `point`, until Sub stops growing. Sub ⊇ Sup-reachable ⇒ borrow regions cover all live points ⇒ conflicts detected.
- **Peers**: AddLivePoint, CheckBorrows/FindLoansThatIntersect (consume the solved regions), getRegionName.
- **Candidates**: (1) **PROBED-not-triggered 2026-06-29**: lines 960/988 `llvm_unreachable("Free region should not grow anymore!")` crash candidate — stressed on the ASSERT build with 5 free-region-growth constructs (store param-borrow into `_Borrow*_Borrow` out-param / into a `struct*_Borrow` field; reborrow-chain return; etc.) — ALL gracefully rc=1 (borrow error emitted BEFORE solve). The invariant is well-protected by pre-solve borrow errors; no construct found that grows a free region. (Would need a CHECK bug + solve to trigger — compound, no hypothesis yet.) (2) **PROBED-SOUND 2026-06-29**: DFS.Copy region propagation across complex CFG — a `&_Mut x` borrow held ACROSS a loop with `x = i` inside REJECTED "cannot assign to x because it is borrowed" (region correctly spans the loop body via back-edge; conflict on the loop-internal write caught). No under-propagation FN; region point-sets fully populated across CFG. (3) the fixpoint `while(changed)` is monotone (regions only grow) → terminates.

## DefUse::VisitReturnStmt (BSCBorrowChecker.cpp:166) — liveness for return value
- **Invariant**: the returned expression is a USE (its borrows/operands are live at the return point); pairs with the lifetime check (PreprocessForParamAndReturn:2220 / region inference) that a returned borrow must outlive the call.
- **Peers**: ActionExtract::VisitReturnStmt (:752, loan/action side), PreprocessForParamAndReturn (:2220, return-borrow-needs-param gate = F81 area), DefUse::VisitUnaryDeref.
- **Candidates**: (1) liveness extraction itself is trivial/correct (Action=Use; Visit(retval)). (2) **PROBED-SOUND 2026-06-30**: `_Safe const int *_Borrow g(int *_Borrow p){ int local=5; return &_Const local; }` → REJECTED "`local` does not live long enough" (region inference catches the dangling local borrow); control `return p` (param borrow) → accepted rc=0. Return-borrow lifetime sound. (3) F81 (return-type-borrow array-blind gate) already filed.

## ActionExtract::VisitReturnStmt (BSCBorrowChecker.cpp:752) — loan/action for return value
- **Invariant**: a tracked (borrow-containing) return value is an Assign to synthetic `__ret` Path with region `CreateFree()` (caller region), so region inference requires the returned borrow's source to outlive the call; untracked return = plain Use.
- **Peers**: DefUse::VisitReturnStmt (:166 liveness), PreprocessForParamAndReturn (:2220 return-borrow-needs-param gate, F81), IsTrackedType (:25), ActionExtract::VisitCStyleCastExpr (:541, F119 loan-record bug).
- **Candidates**: (1) **PROBED-F64/F81 family**: `IsTrackedType(retval type)` false-negative (:756) → return modeled as plain Use, loan not recorded → lifetime check bypassed (F81 array-blind struct-borrow return already filed). (2) **PROBED-SOUND 2026-06-30**: `struct W{const int*_Borrow f;}; w.f=&_Const local; return w;` → REJECTED "`local` does not live long enough" (region inference tracks the field-borrow source region through the by-value struct return). Sound. (3) RNR.isInvalid→Init (:769) edge.

## ActionExtract::VisitArraySubscriptExprOrUnaryDeref (BSCBorrowChecker.cpp:825) — loan path for arr[i]/*p
- **Invariant**: builds the borrow Path for a subscript/deref by deref-ing the BASE — `arr[i]` → Path `*arr` (whole-base deref); the index `i` is NOT part of the path (only a `UsesArraySubscriptNotation` flag).
- **Peers**: DefUse::VisitArraySubscriptExpr (:base=Use whole), VisitUnaryDeref, F109 (array-element loan-kill), F112 (retracted: whole-struct granularity).
- **Candidates**: (1) **F109 classification (source-confirmed)**: array elements share path `*arr` (no per-index distinction) → per-element loan-kill impossible → conservative no-kill = whole-array granularity = the SAME by-design limitation as the RETRACTED F112 (whole-struct). ⇒ F109 likely retract for consistency. (2) the `UsesArraySubscriptNotation` flag's downstream effect on conflict diagnostics — UNPROBED. (3) `*p` (UnaryDeref) shares this path-building — sound (single pointee).

## ActionExtract::VisitUnaryAddrMut/Const (BSCBorrowChecker.cpp:803/779) — borrow creation
- **Invariant**: `&_Mut x`/`&_Const x` record a Borrow action with the region (getRegionName) and BorrowKind (Mut/Shared); the conflict check (elsewhere) enforces "many shared XOR one mutable" over the loan's live region.
- **Peers**: VisitUnaryAddrMutDeref (reborrow `&_Mut *p`), CheckBorrows (:1413 conflict), VisitCStyleCastExpr (:541, F119 cast-reborrow loan bug).
- **Candidates**: (1) creation is symmetric/clean (only BK differs) — sound. (2) **PROBED-SOUND 2026-06-30**: shared+mutable of the same var both-live → REJECTED "cannot borrow x as mutable because it is also borrowed as immutable"; with the shared borrow DEAD before `&_Mut` (NLL) → accepted rc=0. Core "many-shared-XOR-one-mutable over the live region" invariant holds. (3) the AddrMutDeref `&_Mut *&p` reborrow-normalization note — F119-adjacent, already filed.

## ActionExtract::VisitUnaryAddrMutDeref (BSCBorrowChecker.cpp:810) — reborrow &_Mut *p
- **Invariant**: `&_Mut *p` reborrows the pointee → loan Path `*p` (deref of p), so a reborrow conflicts with direct use of p over the reborrow's live region.
- **Peers**: VisitUnaryAddrConstDeref, VisitUnaryAddrMut, VisitCStyleCastExpr (:541, F119 cast-reborrow loan-record bug), CheckBorrows.
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: reborrow `rb=&_Mut *b` then `*b=8` (use b while rb live) → REJECTED "cannot assign to `*b` because it is borrowed". Borrows ARE isPointerType() so the deref Path `*b` is built; reborrow correctly conflicts with direct use of b. Reborrow-through-borrow tracked right. (2) `&_Mut *&p` no-deref note — the `*&` cancellation, sound by design. (3) chained `&_Mut **pp` double-deref path-building.

## ActionExtract::VisitIncrementDecrementOp (BSCBorrowChecker.cpp:374) — p++/++p/p--/--p (all 4 delegate here)
- **Invariant**: `p++` reads then writes the operand; for an `_ArrayElem` borrow it advances within the borrowed array (loan unchanged, whole-array granularity).
- **Peers**: VisitUnaryAddrMut, VisitArraySubscriptExprOrUnaryDeref (F109 array granularity), CheckBorrows.
- **Candidates**: (1) **UNPROBED**: `_ArrayElem` borrow branch (:376) treats `p++` as Use-only (op=RHS) — does the array loan survive so a later array use still conflicts? (consistent with whole-array F109; check conflict preserved). (2) general branch Init(LHS)-then-Use(RHS) order (:382-386) — loan-kill-before-read ordering on `p=p++`-style reassign. (3) owned-pointer `p++` reachability in _Safe (arithmetic restricted).

## RegionCheck::EnsureBorrowSource (BSCBorrowChecker.cpp:2183) — reborrow lifetime constraint
- **Invariant**: a borrow's region must outlive the region of any borrow it reborrows from (AddOutLives BorrowRV→RefRV per borrow-qualified Extension prefix); a Var-rooted source returns (scope-bounded elsewhere).
- **Peers**: RelateRegions, InferenceContext::Solve, Liveness::Compute, PreprocessForParamAndReturn (F-area), F119 (cast-reborrow loan).
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: `int*_Borrow b=&_Mut x; int*_Borrow rb=&_Mut *b; return rb;` (rb reborrows b which borrows local x) → rc=1 "`x` does not live long enough". Region solver (EnsureBorrowSource→AddOutLives→Solve) enforces the reborrow lifetime; dangling-reborrow-return caught. (2) the Var-case early `return` (:2195) — ordering of supportingPrefixes (does returning on Var skip later Extension constraints?). (3) Path types beyond Var/Extension falling through (no constraint).

## BorrowCheck::CheckStorageDead + FindLoansThatFreeze (BSCBorrowChecker.cpp:1503/1513) — storage-end dangling check
- **Invariant**: when a path's storage dies (scope exit), any loan freezing it (a live borrow of it or a prefix) → ForStorageDead diag (borrow must not outlive its referent's storage).
- **Peers**: CheckWrite (shares FindLoansThatFreeze), FrozenByBorrowOf, region solver (EnsureBorrowSource), F119.
- **Candidates**: (1) **PROBED-SOUND 2026-06-30**: `{int x=5; b=&_Mut x;} *b=7;` (x storage dies at block-end, b used after) → rc=1 "`x` does not live long enough". CheckStorageDead + region solver catch the block-scope dangling borrow. (2) **PROBED-SOUND 2026-06-30**: borrow a FIELD `b=&_Mut s.f`, whole-struct `s` storage dies at block-end → rc=1 "`s.f` does not live long enough" (the prefix-freeze in FindLoansThatFreeze catches that borrowing s.f freezes s's storage). Field-borrow×whole-struct-storage-death sound. (3) prefix direction — borrowing a.b.c vs a's storage death.

## DefUse::VisitStmt + ActionExtract::VisitStmt (BSCBorrowChecker.cpp:173, 772) — visitor fallbacks RECURSE
- **Invariant**: both liveness-pass (DefUse) and loan-creation-pass (ActionExtract) fall back to `VisitStmt` which iterates `S->children()` and Visits each. So expression kinds WITHOUT an explicit visitor (ConditionalOperator, InitListExpr for a scalar, CompoundLiteralExpr) are still traversed → borrows created/used inside them are reached.
- **Contrast (why F111 exists in ownership but not borrow)**: `OwnershipImpl::runOnBlock` (BSCOwnership.cpp:2697) uses a NON-recursing allowlist (DeclStmt|CallExpr|assign-BO|inc/dec-UO|ReturnStmt), so bare/condition reads of moved owned slip (F111). The borrow checker has NO analogous top-level filter — its recursion is exhaustive. → borrow-checker visitor-hole surface is LOW.
- **Candidates**: (1) **PROBED-SOUND**: ternary borrow `int *_Borrow r = c ? &_Mut x : &_Mut y;` — BOTH arms' loans tracked (`x=5` AND `y=5` each rejected "cannot assign because it is borrowed"); the recursion creates a loan per arm sharing one Dest r. (2) inherited Action/Kind state across recursion — probed sound for ternary; deeper nesting untested but low-signal given (1). (3) untracked-type early-returns in Visit* (IsTrackedType gates) — separate surface (F64 class).

## RegionCheck::EnsureBorrowSource (BSCBorrowChecker.cpp:2183) — reborrow outlives-source constraint
- **Invariant**: when borrowing `*r` for region 'a where r:&'b, add 'b: 'a (the source ref must outlive the reborrow) via infer.AddOutLives(BorrowRV, RefRV, SuccPoint).
- **Peers**: RelateRegions (:2209), PreprocessForParamAndReturn (:2222), EnsureBorrowSource caller (:1662).
- **Candidates**: (1) UNPROBED: the loop only walks `supportingPrefixes()` and adds OutLives ONLY for Extension prefixes whose `base->ty.isBorrowQualified() && base->D != nullptr` — a reborrow through a NON-decl base (e.g. `*(f())` a temporary borrow, or a base with null D) adds NO outlives constraint → could a reborrow outlive a temporary source? (2) Var prefix `return`s immediately (:2194) — a direct var reborrow adds nothing (correct, var lifetime = scope). (3) nested `**r` (borrow-of-borrow) — does supportingPrefixes yield both levels so both outlives constraints are added? PROBED-elsewhere: reborrow lifetime largely covered by F21/F24/F42.
