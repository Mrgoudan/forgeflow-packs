# Call-Chain Hunting — peer approach to function-by-function reading

Function-by-function reading is good at finding bugs **inside** one function's
logic. After ~14 cycles, that surface is saturating for in-scope features.
The remaining high-yield surface lives at **handoffs**: places where one
function passes data, AST nodes, or invariants to another. Bugs there are
invisible to per-function reading because each function's local invariant
is satisfied — the gap is in the contract between them.

Use **whichever method best fits the next probe**:
- **Func-by-func** (read one function, write invariant + 3 candidates, probe top): good for unread surface, structurally-self-contained logic, dataflow lattices, type-level checks.
- **Call-chain** (pick a chain, trace the AST/data through every hop, find an unmatched peer or asymmetric handoff): good when func-by-func is saturating, or when an existing filed bug hints at a wider family.

**Equal weight.** Neither is "the right" approach. Pick what fits.

---

## Chain lifecycle

Saturation is **version-relative**, not permanent. A chain mined out at
compiler commit X reopens the moment a later commit touches one of its
hop files. So a saturated chain is parked with provenance, never deleted.

| State | Meaning |
|-------|---------|
| `ACTIVE` | Has unprobed hops, or freshly registered. Explorers with `chain:` steering pull from here. |
| `SATURATED @ <commit>` | Every hop probed at compiler commit `<commit>`; yielded its filings (or none). No remaining work *at this compiler version*. Explorers SKIP unless reopened. |
| `REOPENED @ <commit>` | A later compiler change touched a file in this chain's `Hop files:` list → re-walk. Flagged by `scripts/check_chain_reopen.sh`. |

Every saturated chain carries two machine-readable lines:
- `Hop files:` — the source files whose change should reopen the chain.
- `Reopen-if:` — the human-readable trigger condition.

`scripts/check_chain_reopen.sh <commit-range>` intersects each saturated
chain's `Hop files:` with `git diff --name-only <range>` and prints which
chains reopen. It's wired into `scripts/test_pr.sh` Phase E.

---

## Chain registry (state at a glance)

| Chain | Topic | State | Filed |
|-------|-------|-------|-------|
| A | CheckTemporaryVarMemoryLeak / CheckMoveVarMemoryLeak callers | SATURATED @ 28656aa9 (re-walked 2026-05-30; F14 still-live) | F14, F20, F22, F25, F32, F33, F47, F62 |
| B | Prologue / Epilogue / RegionInference matched-pairs | SATURATED @ 34883aa1 (re-walked: VisitBinComma=F11-fix/_Borrow _ArrayElem structurallyEquals/increment-Use all sound; F66 flip-flop = split, _Borrow-inner DO-NOT-FILE) | F09, F16, F38, F40 |
| C | BinAssign across 4 analyzers | SATURATED @ 808187e6 | F18, F19, F45, F46, F48, F55 |
| D | Sema Check* family / fnptr-variance | HEAVILY-MINED @ 28656aa9 (fnptr-variance family — same fix surface now) | F29, F41, F53, F56, F57, **F74, F76** |
| E | CFG element granularity vs AST nesting | SATURATED @ 34883aa1 (re-walked ownership-consumer MaybeSetNull side: sound) | F11, F22 |
| F | getExprPathNullability switch coverage | SATURATED @ 34883aa1 (re-walked: single-root F18/F92 default→Unspecified; CompoundLiteralExpr deref folds-F92) | F18 (all sibling cases fold), F92 |
| G | Dataflow-merge state across CFG joins (C5) | ACTIVE — ownership-merge yielded F75; nullability + init merges SOUND @ 28656aa9 | F26, **F75** |
| H | Heterogeneous `_Safe`/`_Unsafe` redecl compatibility | TRACED 2026-05-30 → **F77** (nested plain-ptr qual drop/swap); F57 fnptr shape still slips | **F77** |
| I | Array-decay-to-`_Borrow` conversion | SATURATED @ 34883aa1 (re-walked: new const/vol/restrict strip add-only/sound; add-volatile-decay FP folds-F102/G15/F27) | — |
| J | Global / static-initializer checking (non-CFG) | TRACED 2026-05-30 → **F78** (static-local union FP). More: see Chain N (CheckInit↔CheckGlobalInit parity) | **F78** |
| K | Two-CFG-build asymmetry (setAllAlwaysAdd vs setAlwaysAdd) | SATURATED @ 34883aa1 (re-walked ownership-side NullCheckInfo consumer: sound) | — |
| L | Mixed-mode `_Safe`/`_Unsafe` overload + fnptr assignment | SATURATED @ 28656aa9 (selection sound at outer level; nested mismatch folds into F76 — selection is a 3rd caller of DoPointerTypesSatisfyAssignmentConstraintsImpl:482) | — |
| M | RewriteBSC rewriter (in-scope slice) | SATURATED @ 28656aa9 (in-scope non-generic rewrites round-trip to well-formed C; F09/F54/F59 the live ones, all filed) | — |
| N | CheckInit ↔ CheckGlobalInit matched-pair parity (nullability init clones) | SATURATED @ 28656aa9 (branch 1 = F78; branches 2/3/4 sound; nested-union folds into F78) | F78 |
| O | Indirect/nested owned-borrow TYPE detection family | SATURATED @ 28656aa9 — family mapped (full recursion-depth table in BSCOwnership.md/SemaBSCOwnership.md); live gaps = F77/F79/F80, + the IsTrackedType:59 fold (co-located F80 peer). Reopen-if any nested-detection predicate changes | F77, F79, **F80** |
| P | Strict vs non-strict pointer-assignment compatibility | SATURATED @ 28656aa9 (non-strict used only on hetero-redecl candidates with identical C types; lax cells unreachable; nested slip folds into F76) | — |
| Q | Borrow-pointer comparison + reborrow checks | SATURATED @ 28656aa9 (compare gates a read-only op = no soundness surface; reborrow predicates are member-`this`-arg only = OOS; user `&_Mut *p` sound) | — |
| R | Safe-zone uninit-at-declaration rules | SATURATED @ 28656aa9 (decl-gate is lax — only rejects _Owned-struct — but every uninit pointer-field USE is backstopped by the init analysis; laxity = filed F01/F07 LOW) | — |
| S | Safe-zone builtin-type conversion matrix | SATURATED @ 28656aa9 (3 verification layers; F71 is the ONLY unsound cell — the one that bypasses the signedness net; F51 enum-path folds) | F71 (+F51) |
| T | Homogeneous redecl param-diff (HasDiff* family) | SATURATED @ 28656aa9 — owned/borrow plain-ptr recursion SOUND at every depth (confirms F77 is hetero-only); the only 2 gaps (fnptr-param/return) are filed F53/F56 (both fold). Reopen-if a FunctionProtoType recursion arm is added | (F53, F56) |
| U | BSCOwnership parallel check-families (checkS/checkOPS/checkBOP × Assign/FieldAssign/FieldUse/Use/DerefAssign) | SATURATED @ 28656aa9 — 3×5 matrix symmetric except the 3 already-filed gaps: F67 (checkSFieldUse lacks the parent-walk OPS/BOP have), F34/F64 (IsTrackedType outer-only), F44/F45 (S-only NullOwnedFields). New `int*_Owned*_Borrow` overwrite leak folds into F34 (co-located peer noted). Reopen-if any check{S,OPS,BOP}* or IsTrackedType changes | (F34/F44/F45/F67, +F123 deref-WRITE residual of F67) |
| V | Borrow-side nested-detection mirror (CheckBorrowOrIndirectBorrowType / CheckNestedBorrowType / hasBorrowFields) | TRACED 2026-05-30 → **F81** (array declarator SemaType.cpp:5202 calls the owned gate but not the borrow twin → borrow-struct array-global accepted → UAF). Borrow PREDICATE is sound (hasBorrowFields recurses); the gap was a missing CALL SITE. Co-located LOW peer F82 (static-local placement-gate gap, SemaDecl.cpp:8525 isFileContext guard — backstopped, not filed). One untraced cell left: CheckNestedBorrowType (borrow-only, from BuildPointerType) | **F81** |
| W | Init-analysis dest-Place projection coverage (transferStatement Assign) | TRACED 2026-05-30 → F83 | F83 |
| X | Nullability path-fact invalidation on mutation events (reassign/field-write/call/addr-taken) | SATURATED @ 28656aa9 — all 4 events traced: F84 (reassign, VisitBinaryOperator:613-631), F85 (field-write deeper-stale, :632-647), F87 (call-mutate, VisitCallExpr:654-668), F89 (addr-taken `&_Mut p`, VisitUnaryOperator:674-683 missing UO_Addr* arm). One `InvalidateAllStatusForVar(VD)` helper closes all 4. Reopen-if VisitBinaryOperator/VisitCallExpr/VisitUnaryOperator narrowing handling changes | F84, F85, F87, F89 |
| Y | Init-analysis terminator-operand coverage (InitAnalysis::run terminator switch) | TRACED 2026-05-30 → F88 + F90 | F88, F90 |
| Z | form-based visitors omit comma / pointer-arith opcodes (F91/F92/F93 cross-analyzer pattern) | SATURATED | F91,F92,F93 |
| CE1 | SafeExpr transparent-wrapper coverage across CodeGen emission emitters (Scalar/Agg/Complex/ConstRValue/ConstLValue) | TRACED | F60/F63/G09 |
| AA | isNullExpr null-classification consumed divergently by ownership vs nullability | TRACED 2026-06-29 → F108 (3 ownership consumer sites VisitDeclStmt:2357/VisitBinAssign:2201/HandleInitListExpr:2441 all fold; one isNullExpr DeclRefExpr-arm fix closes all) | F108 |
| AB | getMemberFullField member-path extraction (ownership-side path consistency) | SHAPE-BLOCKED @34883aa1 (no in-scope subscript over owned-field structs: array + _ArrayElem of owned-containing struct both type-rejected; OOS-generic only) | — |
| AC | ActionExtract Action-kind classification (Assign/Init/Use/Noop) consistency in the borrow checker | ACTIVE | — |
| AD | owned/borrow placement gates + has{Owned,Borrow}Fields array-field recursion | PROBED-F124 | F124 |
| AE | isArrayElemQualified array-elem qualifier across borrow-checker (DefUse/ActionExtract) + Sema safe-zone (AreBSCPointerQualifiersCompatible/IsSafePointerConversion) | TRACED 2026-06-30 — SOUND: _ArrayElem-DROP conversion (`T*_Borrow`←`T*_Borrow _ArrayElem`, allowed) PRESERVES the loan; reborrow tracks the array regardless of qualifier (mutate-while-live + 2nd-mut-borrow both caught). Adding _ArrayElem restricted (IsSafePointerConversion). No cross-component divergence. | — |
| AF | IsCastFromVoidPointer void->typed owned RECOVERY (__take_from_raw round-trip) consumed at 4 ownership TransferFunctions sites | TRACED 2026-06-30 — OOS-ADJACENT: gates `(T*_Owned)(void*_Owned)` typed-from-void recovery = __take_from_raw direction (OUT of scope); the in-scope `(void*_Owned)p` forward cast (safe_free) is not gated by it. Deprioritized. | — |
| AG | hasOwnedFields owned-field detector (Type::hasOwnedFields TypeBSC.cpp:72) array-blindness — F81-twin; consumed at 5 ownership sites + Sema CheckBSCQualTypeAssignment | TRACED 2026-06-30 — LATENT-UNREACHABLE: Type::hasOwnedFields IS array-blind (RecordType+PointerType only, returns false for ArrayType) BUT the owned decl gate CheckOwnedOrIndirectOwnedType rejects array-of-owned-field-struct (local+global both rc=1 'cannot be qualified by _Owned even indirectly') so the blindness has no constructable trigger. Unlike F81 (borrow decl gate HAD the hole). isMoveSemanticTypeImpl (TypeBSC.cpp:362) is ALSO array-blind but ALSO latent (owned-array gate comprehensive: variable+global+struct-FIELD all rc=1). | F80/F81-adjacent |
| AH | Nullability variance checks don't recurse into fnptr pointees where owned variance checks do | PROBED | F125,F126 |
| AI | Nullability flow-narrow must be invalidated on EVERY state-changing op, not just direct assignment | PROBED | F127 |
<!-- CHAIN-REGISTRY-END: scripts/add_chain.sh inserts new registry rows immediately ABOVE this line. Do not move. -->

SATURATED chains keep their full entry below for the hop ledger + reopen
trigger; Explorers skip them unless `check_chain_reopen.sh` flags a reopen.

**Audited-SOUND surfaces this session (2026-05-29/30, negatives — not full chains, but
recorded so they aren't re-walked):** `_Safe`/`_Unsafe`-block ledger boundary (analyzers
are safe-zone-agnostic, whole-CFG); nullability narrowing PRODUCER (only F70 + F33 live;
combinator dead except under `!`); init-analysis InitLattice merge (correctly MEETs);
raw-transfer builtins `handleBSCRawTransferBuiltin` (all qual dimensions preserved);
CodeGen owned-drop (sound BY CONSTRUCTION — no implicit drops, explicit-free-only);
C-style cast borrow-drop (`_Unsafe`-only explicit-cast escape — NOT a `_Safe` FN, not filed);
`IsSafePointerConversion`; pointer `++`/`--` rework; `_ArrayElem` move/free/borrow tracking.
See `_probed.md` 2026-05-29/30 for the per-probe ledgers.

## Active chains

### Chain A — `CheckTemporaryVarMemoryLeak` / `CheckMoveVarMemoryLeak` callers

**Status: SATURATED @ 808187e6 (2026-05-27).**
**Hop files:** `clang/lib/Sema/BSC/SemaBSCOwnership.cpp`, `clang/lib/Sema/SemaExpr.cpp`, `clang/lib/Sema/SemaExprMember.cpp`, `clang/lib/Sema/SemaStmt.cpp`, `clang/lib/Parse/ParseExpr.cpp`, `clang/lib/Parse/ParseDecl.cpp`.
**Reopen-if:** any commit touches `CheckTemporaryVarMemoryLeak` / `CheckMoveVarMemoryLeak` or any of the 6 call sites below.

Hop ledger:
| call site | shape probed | outcome |
|-----------|--------------|---------|
| SemaExprMember.cpp (member base) | `(struct B){.p=mk()}.p`, paren/cast wrappers | F20, F32→F20, **F47** (CompoundLiteralExpr not in recognized-temp set) |
| SemaExpr.cpp:17120 (`*expr`) | `sizeof(*mk())` unevaluated | F37 (LOW family), F22 |
| SemaExpr.cpp (call arg) | `f((void)mk())` | folds into F14 |
| predicate body `dyn_cast<CallExpr>` | paren/cast/comma/cond wrappers | F14 (IJOAO8) |
| CheckMoveVarMemoryLeak `IgnoreParenCasts` | `_Unsafe(s->f)` SafeExpr wrapper | **F62** (SafeExpr-strip, class C10) |
| ParseExpr / ParseDecl init | decl-init RHS | folds into F14 |

The predicate `CheckTemporaryVarMemoryLeak` lives in
`SemaBSCOwnership.cpp:534-545`. It's called from at least 5 sites:
- `ParseExpr.cpp:646` (assignment RHS during parsing)
- `SemaStmt.cpp:3937` (return statement)
- `SemaExpr.cpp:6728` (call argument)
- `SemaExpr.cpp:17120` (`*expr` Sema-level UO_Deref handling — F37 root)
- `SemaExprMember.cpp:1288` (member access base — F20/F32 root)
- `ParseDecl.cpp:2517` (decl initializer)

For each call site, ask: does the site pre-wrap the expression (e.g.,
`IgnoreParens`) before passing it, or does it pass the raw AST? Does the
predicate's `dyn_cast<CallExpr>(E)` (no wrappers stripped) see what it
needs to see?

Filed exemplars in this chain: **F14, F20, F22, F32 (folded into F20)**.

The mirror predicate `CheckMoveVarMemoryLeak` has its own callers — same
audit applies. Filed: **F21, F25, F33**.

### Chain B — Prologue / Epilogue / RegionInference matched-pairs

**Status: SATURATED (in-scope) @ 28656aa9 (2026-05-29).** The in-scope escaping
hops (`AtomicExpr`, `VAArgExpr`, `OpaqueValueExpr`, `PseudoObjectExpr`) are now
characterized as **sound-by-construction**: they evaluate ALL operands, so the
borrow checker's generic `VisitStmt` child-iteration sees borrows/moves inside
them correctly even though Prologue does not hoist them (verified: a mut-borrow
conflict AND an owned-move buried inside `__c11_atomic_*` operands are both
caught — `_probed.md` 2026-05-29). The F16 root cause needs CONDITIONAL eval
(UNSELECTED arms), which only the OOS kinds (`_Generic`/`ChooseExpr`/GNU `?:`)
have. Round-trip (Prologue hoist → Epilogue restore → codegen) verified clean via
`-rewrite-bsc` across all common positions (no surviving `_borrowck_tmp`).
Yielded F09/F16/F38/F40. **Reopen-if:** a `Transform*` override is added/changed
in SemaDeclBSC.cpp, or a new all-operands-evaluated escaping kind appears.
**Hop files:** `clang/lib/Sema/BSC/SemaDeclBSC.cpp` (Prologue/Epilogue), `clang/lib/Analysis/BSC/BSCBorrowChecker.cpp` (RegionInference).

`BorrowCheckerPrologue::Transform_X` rewrites certain AST kinds into
hoisted `_borrowck_tmp_N` form so downstream analyzers see a simpler
shape. **For every Transform_X**, three peers must exist:
1. `BorrowCheckerEpilogue::Transform_X` — restore the original AST form.
2. `RegionInference` reconnection — the hoisted temp's region must be
   related to the original expression's region.
3. Codegen / rewriter equivalent — the side-effecting expression must
   still emit its effect.

Filed exemplars in this gap: **F09** (codegen drop of `_Generic` /
`__builtin_choose_expr` selected arm side effects), **F16** (borrow
checker walks UNSELECTED arms — Prologue normalization missing),
**F38** (region-inference doesn't relate `_borrowck_tmp_0` to the
selected arm's source region), **F40** (Prologue hoists CallExpr side
effects out of `&&`/`||`/`?:` operands — analyzer-only false positive,
runtime correct).

Known AST kinds that **escape Prologue hoisting** (from `_playbook.md`):
- `ChooseExpr` (`__builtin_choose_expr`)
- `GenericSelectionExpr` (`_Generic`)
- `BinaryConditionalOperator` (GNU `a ?: b`)
- `AtomicExpr`, `VAArgExpr`, `OpaqueValueExpr`, `PseudoObjectExpr`

Out-of-scope-by-user: ChooseExpr, GenericSelectionExpr, BinaryConditionalOperator. In-scope: AtomicExpr, VAArgExpr, OpaqueValueExpr, PseudoObjectExpr.

### Chain C — BinAssign across 4 analyzers

**Status: SATURATED @ 808187e6 (2026-05-27).**
**Hop files:** `clang/lib/Analysis/BSC/BSCOwnership.cpp`, `clang/lib/Analysis/BSC/BSCBorrowChecker.cpp`, `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp`, `clang/lib/Analysis/BSC/BSCIRInitAnalysis.cpp`.
**Reopen-if:** any commit touches a `VisitBinaryOperator` / `VisitBinAssign` / `transferStatement` assignment arm in any of the four analyzers.

Hop ledger:
| analyzer hop | shape probed | outcome |
|--------------|--------------|---------|
| Ownership::VisitBinaryOperator | `s.f = X` then read; `(s).f=` paren | F19, **F45** (stale SNullOwnedFields), F46 (getMemberFullField paren) |
| Nullability::VisitBinaryOperator | `p += i` compound-assign | **F48** (IJOTZ9 — RHSKind from integer operand) |
| Borrow::ActionExtract::VisitBinAssign | `(s).f = q` paren compound-literal RHS | folds into F46 |
| Init::transferStatement | `s.f->g = ...` where `s.f` uninit | **F55** (getFieldPath truncation) |
| getExprPathNullability (shared by Nullability hop) | `p + 1`, `q=(int*_Borrow){nullable}` | F18 + folds (see Chain F) |

### Chain C (original notes follow)

A single `BinaryOperator(BO_Assign)` AST node is visited by every analyzer:
- `Ownership::TransferFunctions::VisitBinaryOperator` (BSCOwnership.cpp)
- `BorrowCk::DefUse::VisitBinaryOperator` + `ActionExtract::VisitBinaryOperator` (BSCBorrowChecker.cpp)
- `NullabilityCheck::TransferFunctions::VisitBinaryOperator` (BSCNullabilityCheck.cpp)
- `InitAnalysis::transferStatement` (BSCIRInitAnalysis.cpp, via BSCIR's Assign)

They should agree on the safety verdict for any specific assignment.
Each disagreement is a latent bug. Cycle 14 found one (init OK via
ensure_init, ownership uninit-use diag).

Audit method: enumerate assignment shapes (`p = q`, `p->f = q`, `(*p).f = q`,
`(s).f = q`, `arr[i] = q`, `*(p+1) = q`, comma'd, ternary'd) — for each,
construct a minimal probe and verify all four analyzers agree.

### Chain D — Sema Check* family parallel audit

**Status: HEAVILY-MINED @ 28656aa9 (2026-05-29).** Yielded **F74** (call-site dispatch gate
`isFunctionPointerType()` skips ptr-to-fnptr — SemaExpr.cpp:10329) and **F76** (fnptr-variance
check drops qualifiers on nested fnptr params via the canonical-pointee compare —
DoPointerTypesSatisfyAssignmentConstraintsImpl:482) this session, on top of F29/F41/F53/F56/F57.
The fnptr-variance family now largely shares fix surfaces — further variants FOLD. The
heterogeneous-redecl compatibility path is split out as **Chain H** (still untraced, distinct caller).
**Hop files:** `clang/lib/Sema/BSC/SemaBSCOwnership.cpp`, `clang/lib/Sema/BSC/SemaDeclBSC.cpp`, `clang/lib/Sema/SemaExpr.cpp` (the call-site gate), `clang/lib/Sema/BSC/SemaBSCSafeZone.cpp` (the pointee compare).

Sibling check functions live in `SemaBSCOwnership.cpp` and `SemaDeclBSC.cpp`:
- `CheckOwnedQualTypeAssignment` (SemaBSCOwnership.cpp:347, 406)
- `CheckBorrowQualTypeAssignment` (SemaBSCOwnership.cpp:677, 713)
- `CheckNullabilityQualTypeAssignment` (SemaDeclBSC.cpp:156, 188)
- `CheckOwnedFunctionPointerType` (SemaBSCOwnership.cpp:440)
- `CheckBorrowFunctionPointerType` (SemaBSCOwnership.cpp:854)
- `CheckBSCFunctionPointerType` (SemaBSCOwnership.cpp:511)
- `CheckEnsureInitFunctionPointerType` (SemaBSCOwnership.cpp:924)

Each checks one variance dimension on (assignment OR function pointer
assignment). Filed exemplar: **F29** — `CheckBSCFunctionPointerType`
checks owned/borrow but doesn't recurse into nullability variance the
way the assignment version does.

Audit method: tabulate (function × dimension × wrapper-handling-depth ×
recursion-style). Asymmetric cells are candidates.

Yielded F41 IJOPV7 (CheckOwnedFunctionPointerType only outer-level; peer
CheckBorrowFunctionPointerType recurses into pointees via
BorrowParamTypesMatch lambda).

### Chain E — CFG element granularity vs AST nesting

**Status: SATURATED @ 28656aa9 (re-walked 2026-05-29).** Both CFG builds SOUND on the reachable
in-scope surface (see re-walk note below). The two-CFG-build ASYMMETRY itself (ownership/null
raw-AST CFG vs borrow Prologue CFG) is split out as **Chain K** (untraced). Yielded F11 (comma),
F22 (call-in-control-flow-cond).
**Hop files:** `clang/lib/Sema/BSC/SemaDeclBSC.cpp` (setAlwaysAdd list), `clang/lib/Analysis/BSC/BSCBorrowChecker.cpp` (CFG element visitors).

The borrow checker iterates CFG elements; nested side-effecting
sub-expressions that aren't their own CFG element escape its visitors.
F11 (comma operator) is the canonical case.

Reference: **TWO CFG configs** —
- `SemaDeclBSC.cpp:273` `setAllAlwaysAdd()` for Nullability + Ownership, on the
  **RAW AST** (no Prologue). ALL Stmt classes (incl. CallExpr) are linearized
  into their own CFG elements → moves never hidden in sub-expressions.
- `SemaDeclBSC.cpp:1494-1504` restricted 10-class list (BinaryOperator, Break,
  Compound, Decl, Do, For, If, Return, Switch, While) for BorrowCheck, on the
  **Prologue-transformed AST**. The Prologue (`TransformBinaryOperator` :844 etc.)
  recursively hoists every side-effecting subexpr into a temp DeclStmt.

**Re-walked 2026-05-29 (bsc-explorer):** both configs are SOUND on the reachable
in-scope surface. Probed: &&/|| operand use, for-cond/do-cond/for-inc-comma
after-use, owned move in subscript index, owned move in ?: arm, owned ptr to
_Bool param — ALL caught or shape-rejected (see `_probed.md` 2026-05-29). The
only escapees remain comma VALUE-flow (F11) and conditional eval (F16/F25). A
real-but-benign source smell found: BSCIRBuilder &&/|| lowering (:786) does NOT
force Move→Copy the way the comparison branch (:838) does → an `_Owned` operand
of `&&` lowers as a Move, but this is init-only and produces no observable FP/FN
(leak detection is AST-based in BSCOwnership, not BSCIR). Logged in BSCIRBuilder.md.

Filed exemplars: **F11** (comma value-flow), **F22** (call-in-control-flow-cond).

### Chain F — `getExprPathNullability` switch coverage audit

**Status: SATURATED @ 808187e6 (2026-05-27).** Every sibling missing-case
folds into F18 (one fix surface — the BinaryOperator/Expr switch).
**Hop files:** `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp`.
**Reopen-if:** a commit changes the `getExprPathNullability` switch (lines ~307-423) — e.g. adds a case (might fix F18) or a new pointer-producing Expr kind needs handling.

Hop ledger:
| missing case | outcome |
|--------------|---------|
| BO_Add / BO_Sub (pointer arith) | **F18** (IJOEWJ) |
| BO_AddAssign / BO_SubAssign | folds into F18 (+ F48 on the state-update side) |
| CompoundLiteralExprClass | folds into F18 (Explorer #6 confirmed) |
| AtomicExpr / OpaqueValueExpr / PseudoObjectExpr / VAArgExpr | unprobed but all fold (same default→Unspecified path) |
| StmtExpr (GNU) | out-of-scope keyword |

A single switch on `Expr::getStmtClass()` in
`clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp:307-423` returns the
path-nullability of an arbitrary expression. Every analyzer site that
decides "should I emit a Nullable→NonNull diag?" gates on this switch
returning `NullabilityKind::Nullable`. Any AST kind that produces a
pointer value but is absent from the switch falls through to default →
returns `Unspecified` → silently launders Nullable.

Filed exemplars:
- **F18** (IJOEWJ) — BinaryOperatorClass case handles BO_Comma/BO_Assign
  but NOT BO_Add/BO_Sub (pointer arithmetic).

Confirmed folds of F18 (NOT filed separately — same fix surface):
- `CompoundLiteralExprClass` missing entirely — `q = (int *_Borrow){nullable_p}`
  silently launders Nullable. Confirmed by Explorer #6, 2026-05-21.
  Logged in `_probed.md`.

Unprobed sibling missing-cases in the same switch (would also produce
Unspecified for any pointer-typed result):
- `AtomicExpr` — atomic-load result.
- `OpaqueValueExpr` — substitution placeholder.
- `PseudoObjectExpr` — ObjC-style pseudo-property.
- `VAArgExpr` — variadic argument extraction.
- `StmtExpr` (GNU `({...})`) — out-of-scope per keyword list.
- `OffsetOfExpr` — when used as pointer difference.

Audit policy: this chain is a **single-function switch coverage audit**.
Probing additional missing cases would all fold into F18. Only probe if
the eventual F18 fix is narrow (BO_Add/BO_Sub only) and a comprehensive
audit-issue is warranted. Otherwise, treat sibling cases as folded into
F18.

Reference: BSCNullabilityCheck.cpp:307-423. Distinct from Chain B
(matched Prologue/Epilogue pairs), Chain C (BinAssign across analyzers),
Chain E (CFG element granularity) — Chain F is intra-function switch
audit.

### Chain G — Dataflow-merge state across CFG joins (C5)

**Status: ACTIVE.** Ownership-merge yielded **F75**; the nullability and init merges are
SOUND at 28656aa9.
**Hop files:** `clang/lib/Analysis/BSC/BSCOwnership.cpp` (OwnershipImpl::merge), `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp` (mergeVD/mergeFP/mergeDPVD), `clang/lib/Analysis/BSC/BSCIRInitAnalysis.cpp` (meetStates).
**Reopen-if:** any commit touches a merge/meet function or the lattice in one of the three analyzers.

Each analyzer reconciles per-predecessor state at a CFG join. SOUND merge = MEET
(conservative: "maybe-moved"/"maybe-uninit"/"Nullable" if any pred says so). Hop ledger:
| analyzer merge | shape probed | outcome |
|----------------|--------------|---------|
| `OwnershipImpl::merge` (BSCOwnership.cpp:254-276) | field moved on one branch, consumed after join | **F75** — UNIONs the owned-field set instead of MEETing → double-free. Same union at :228-242 (OPS), :266-269 (SNull), :288-301 (BOP) = same fix surface (fold). |
| `mergeVD`/`mergeFP` (BSCNullabilityCheck.cpp:844-892) | absent-key asymmetry across preds | SOUND — `initStatus` pre-populates every Nullable key and never erases, so the absence branch is unreachable (this is exactly why F26 is DPVD-only). |
| `mergeDPVD` | DerefPath present on one pred only | **F26** (filed prior) — DPVD not pre-populated → absent-key dropped. |
| `meetStates` (BSCIRInitAnalysis.cpp:329-381) | field init on one arm only, used after join | SOUND — MEETs to MaybeInit → use rejected. The one over-approx (union-field write) is `_Unsafe`-gated dead code. |

### Chain H — Heterogeneous `_Safe`/`_Unsafe` redeclaration compatibility — **UNTRACED**

**Status: TRACED 2026-05-30 → yielded F77.** The `AreParamTypesCompatible` lambda
(TypeBSC.cpp:235-286, added by 584c8ae) strips owned/borrow/arrayelem then re-checks via
OUTER-ONLY `AreOwnedBorrowQualifiersCompatible` (:154-181) → a BSC qualifier nested one
plain-pointer level deep (`int*_Owned*` vs `int**`/`int*_Borrow*`) is dropped/swapped silently
on param AND return (**F77**, HIGH double-free). Homogeneous redecl correctly rejects it (F53
path recurses); only the hetero path leaks. STILL OPEN on this chain: the explorer re-confirmed
F57's fnptr-buried shape ALSO still slips (584c8ae's recursion never fires for it) — that's F57
(filed), but the chain's fix must cover BOTH the plain-pointer (F77) and fnptr (F57) nestings.
**Hop files:** `clang/lib/AST/BSC/TypeBSC.cpp` (`areFunctionTypesCompatibleForHeterogeneousRedecl` :235-286 → `AreOwnedBorrowQualifiersCompatible` :154-181), the MergeFunctionDecl redecl-merge caller.
**Reopen-if:** any commit touches `areFunctionTypesCompatibleForHeterogeneousRedecl` / `AreOwnedBorrowQualifiersCompatible` or the mixed-mode redecl merge.

When the same function has BOTH a `_Safe` and an `_Unsafe` declaration, the mixed-mode rules
(skill §5: a `_Safe` decl may *add* `_Owned`/`_Borrow`/`_ArrayElem` to a raw param/return,
must NOT *remove* them nor *swap* `_Owned`↔`_Borrow`; return-type C-quals preserved, param
C-quals stripped) must hold **through nested types**. This is the REDECLARATION analog of
F74/F76 (which hit the ASSIGNMENT path) and a DIFFERENT caller from F53/F57. Audit: tabulate
each allowed/forbidden qualifier change × nesting (pointee, fnptr-param, fnptr-return,
array-elem) and probe a heterogeneous redecl pair where the nested change should be rejected
but is accepted (or vice versa). Highest-priority untraced chain.

### Chain I — Array-decay-to-`_Borrow` conversion matrix — **UNTRACED**

**Status: SATURATED @ 28656aa9 (traced 2026-05-30).** SOUND. Structural finding: when
`isBorrowArrayDecayTypeMatch` returns true, `GetSafeArrayDecayType` returns `DestPtrType`
verbatim, so the downstream `IsSafePointerConversion(decayed, dest)` compares dest==dest — a
TAUTOLOGY hop. All soundness of the matched path rests on `isBorrowArrayDecayTypeMatch`
(SemaExpr.cpp:545-576) alone, which is sound: its strip only ADDs const/volatile (never drops),
element-type mismatch rejected, trivial→void correctly excludes pointer-field structs, _ArrayElem
requirement enforced. ~22 cells all SOUND/shape-rejected (see _probed.md + SemaBSCSafeZone.md).
**Hop files:** `clang/lib/Sema/BSC/SemaBSCSafeZone.cpp` (`GetSafeArrayDecayType` :835), `clang/lib/Sema/SemaExpr.cpp` (`isBorrowArrayDecayTypeMatch` :545-576, `MaybeDecayArrayToBorrowArrayElemPointer` :604-632).
**Reopen-if:** a commit touches `isBorrowArrayDecayTypeMatch` / `GetSafeArrayDecayType` / the decay branch of `IsSafeConversion`.

`T[N]` decaying to `T*_Borrow` / `T*_Borrow _ArrayElem`. `IsSafePointerConversion` (the
pointer→pointer matrix) was audited SOUND this session, but the array→pointer DECAY matrix
was not. Audit: tabulate (element type × dest borrow/arrayelem/const × N) and find a decay
that launders an element-type or qualifier (e.g. `const T[N]` → `T*_Borrow` dropping const,
or an element-type mismatch accepted). Distinct fix surface from IsSafePointerConversion.

### Chain J — Global / static-initializer checking (non-CFG path) — **UNTRACED**

**Status: TRACED 2026-05-30 → yielded F78.** The function-static-local init path (`CheckInit`,
BSCNullabilityCheck.cpp:488-493, `!Init` branch) and the global path (`CheckGlobalInit`,
SemaDecl.cpp:14816) are matched-pair clones that MUST agree. The 09074459 union-narrowing fix
landed in CheckGlobalInit's `!Init` branch + both peers' `NumInits==0` branch, but NOT in
CheckInit's `!Init` branch → a static-local default-init union with a non-`_Nonnull` first field
is wrongly rejected while the global form is clean (**F78**, MED FP, C6). The remaining parity
branches are split out as **Chain N**. Note: F66 lives on `CheckNullabilityQualTypeAssignment`
(a different, FN, surface) — not re-hit.
**Hop files:** `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp` (CheckInit, FindNonnull), `clang/lib/Sema/SemaDecl.cpp` (CheckGlobalInit), `clang/lib/Sema/BSC/SemaDeclBSC.cpp` (`CheckNullabilityQualTypeAssignment`).
**Reopen-if:** a commit touches the global/static init nullability/owned check or `CheckNullabilityQualTypeAssignment`.

Global/static/file-scope initializers DON'T go through the function-CFG dataflow — they use a
separate Sema-time recursion. A nested global init (struct-with-`_Nullable`-field, union init,
array-of-pointers, nested struct) could mis-check a nullability/owned qualifier the CFG path
would catch. Both fix commits are recent. Audit: probe global/static initializers with nested
nullable/owned/borrow fields and a value that violates the qualifier; compare to the
function-local form (which goes through the CFG dataflow).

### Chain K — Two-CFG-build asymmetry

**Status: SATURATED @ 28656aa9 (traced 2026-05-30).** NEGATIVE. `setAllAlwaysAdd()` (ownership +
nullability, raw AST) full-linearizes EVERY Stmt class into its own CFG element — a strict SUPERSET
of the borrow Prologue-CFG's element boundaries. `OwnershipImpl::runOnBlock` (BSCOwnership.cpp:2603)
visits only 5 kinds (DeclStmt/CallExpr/assign-BinOp/incdec-UnaryOp/ReturnStmt), but because every
nested side-effecting subexpr is ITS OWN element, the filter still reaches a `consume()`/move buried
in a `?:`/comma/if-cond/`&&`. So the two-build asymmetry is BENIGN — ownership/null never hide what
borrow sees. The lone laundering construct is comma = **F11** (a single-build borrow-checker defect,
not a cross-build divergence; ownership special-cases BO_Comma at :2177). 8 probes, vg_probe-confirmed.
**Hop files:** `clang/lib/Sema/BSC/SemaDeclBSC.cpp` (`setAllAlwaysAdd()` :273 for ownership+nullability; the 10-class `setAlwaysAdd` :1495-1504 for borrow), the two `AnalysisDeclContext` configs.
**Reopen-if:** a commit changes either CFG config or the Prologue.

Ownership + nullability run on a CFG built with `setAllAlwaysAdd()` over the **raw AST** (full
linearization). The borrow checker runs on a CFG built with the restricted 10-class list over
the **Prologue-transformed AST**. The borrow side was checked (Chain E); the ASYMMETRY itself
is untraced — a construct where the two builds disagree on element granularity so one analyzer
sees an effect (a move / a borrow / an init) the other doesn't, OR where a Prologue temp that
ownership never sees (it runs on the raw AST, no Prologue) changes the verdict. Audit: find a
shape where ownership/nullability (raw-AST CFG) and borrow (Prologue CFG) reach different
element boundaries for the same source.

### Chain L — Mixed-mode `_Safe`/`_Unsafe` overload resolution + fnptr assignment — **UNTRACED**

**Status: SATURATED @ 28656aa9 (traced 2026-05-30).** SOUND. `SelectDeclForHeterogeneousRedecl`
(SemaBSCSafeZone.cpp:236-285) picks the correct view at the OUTER qualifier level in every cell
(safe/unsafe × only-safe/only-unsafe/both × direct-call/fnptr-assign/fnptr-call) — owned/borrow/raw
mismatches on param and return are caught by `AreBSCPointerQualifiersCompatible` (outer) and the
SZ-spec mismatch at :626. `CheckIsUnsafeOverloadCall` is OOS (operator-overload only). The ONE
accept-that-should-reject (nested `int*_Owned*` view → `int**` dest fnptr) routes through
`DoPointerTypesSatisfyAssignmentConstraintsImpl:482` — **F76's exact root** (selection is a THIRD
caller of it). FOLD, not new. When F76 is fixed, verify the fix also closes the
`SelectFunctionDeclForPointerAssignment`/`DoesFunctionPointerSatisfyConstraints` path.
**Hop files:** `clang/lib/Sema/BSC/SemaBSCOverload.cpp`, `clang/lib/Sema/BSC/SemaBSCSafeZone.cpp` (`SelectDeclForHeterogeneousRedecl`, `DoPointerTypesSatisfyAssignmentConstraintsImpl`).
**Reopen-if:** a commit touches BSC overload resolution, `SelectDeclForHeterogeneousRedecl`, or `DoPointerTypesSatisfyAssignmentConstraintsImpl`.

The rule (skill §6): a `_Safe` fnptr may only be assigned from a function that HAS a `_Safe`
declaration; in safe context only the `_Safe` overload is callable. Untraced: whether overload
SELECTION across `_Safe`/`_Unsafe` decls ever lets a `_Safe` fnptr bind to a function lacking a
`_Safe` decl, or picks the wrong overload's qualifiers, through some selection path. Audit:
mixed-mode declared functions, fnptr assignment + call in safe vs unsafe context.

### Chain M — RewriteBSC rewriter (in-scope slice) — **UNTRACED**

**Status: ACTIVE — UNTRACED. F09/F54 lived here.**
**Hop files:** `clang/lib/Frontend/Rewrite/RewriteBSC.cpp` (`RewriteNonGenericFuncAndVar`, `RewriteDecls`, `RewriteTypeDefinitions`).
**Reopen-if:** a commit touches RewriteBSC.cpp or the temp-substitution.

`-rewrite-bsc` lowers BSC AST to plain C text. F09 (undefined `_borrowck_tmp_N` in output) and
F54 (RewriteNonGenericFuncAndVar) lived here. The Prologue/Epilogue round-trip was verified
clean this session for common constructs, but the rewriter's OWN dispatch (decl/type/typedef
rewriting, include collection, macro directives) is untraced. Lower priority — much of the file
is generic-instantiation (OOS); the in-scope slice is non-generic func/var/record/typedef
rewriting. Detector: `-rewrite-bsc` output + grep for undefined `_borrowck_tmp` / malformed C.

### Chain N — CheckInit ↔ CheckGlobalInit matched-pair parity (nullability init clones)

**Status: SATURATED @ 28656aa9 (traced 2026-05-30).** Branch (1) `!Init` union-narrowing = **F78**
(filed). Branches (2)(3)(4) SOUND: (2) the `GetExprNK` vs `getExprPathNullability` helper differences
are benign (raw-transfer builtin propagates nullability onto the result type) or global-unreachable
(globals need a constant-expr RHS, no flow state); (3) `NumInits==0` is symmetric (09074459 fix in
both peers); (4) array recursion reads element `_Nonnull` identically. The nested-union-via-struct
form folds into F78 (same `!Init` fix surface). `VD->getInit()` always delivers the SEMANTIC form so
CheckInit's syntactic field-index is correct. 8 probes.
**Hop files:** clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp (CheckInit :483-540, FindNonnull :256-282), clang/lib/Sema/SemaDecl.cpp (CheckGlobalInit :14812+).
**Reopen-if:** a commit touches CheckInit / CheckGlobalInit / FindNonnull or the union/nested-init nullability check.

CheckInit (Analysis, function-CFG path) and CheckGlobalInit (Sema, global path) are near-identical CLONES across two files. They MUST agree for the same init. Four structural branches to compare for parity: (1) `!Init` default-init branch [= F78, the union-narrowing fix from 09074459 missing in CheckInit], (2) pointer-RHS nullability via GetExprNK vs getExprPathNullability, (3) `NumInits==0` ILE branch, (4) array recursion via getAsArrayType vs getAsArrayTypeUnsafe. Audit: for each branch, a static-local vs file-scope differential on the same nested nullable init. Branch (2) PROBED-SOUND 2026-06-25 (explicit-init path): file-scope `struct S gs={(int*)0}` and static-local `static struct S ls={(int*)0}` BOTH reject "nonnull cannot be assigned by nullable" — parity holds for explicit _Nonnull-field=null init. F78 (branch 1, default-init union) remains the only asymmetry.

### Chain O — Indirect/nested owned-borrow TYPE detection family

**Status: ACTIVE — high priority.**
**Hop files:** clang/lib/AST/BSC/TypeBSC.cpp (hasOwnedFields/hasBorrowFields :57-99, isNestedBorrow, AreOwnedBorrowQualifiersCompatible), clang/lib/Sema/BSC/SemaBSCOwnership.cpp (CheckOwnedOrIndirectOwnedType, CheckBorrowOrIndirectBorrowType, CheckNestedBorrowType), clang/lib/Sema/BSC/SemaBSCSafeZone.cpp.
**Reopen-if:** a commit touches any 'does this type contain owned/borrow nested' predicate.

EXEMPLARS F77 (heterogeneous-redecl AreParamTypesCompatible outer-only) and F79 (CheckMoveVarMemoryLeak isOwnedQualified-only, missing isMoveSemanticType) both = a nested/indirect owned-or-borrow obligation NOT detected because the predicate checks only the OUTER level / only isOwnedQualified. AUDIT every predicate in this family for the same gap: does it recurse into pointee / fnptr-params / struct-fields / array-elements, and does it test isMoveSemanticType (struct-with-owned-field) vs only isOwnedQualified? hasOwnedFields/hasBorrowFields recurse RecordType+PointerType but NOT FunctionProtoType (the F57/FindSafeFeatures gap). Tabulate (predicate x nesting x owned/borrow/move-semantic) and probe outer-only cells. Likely MORE bugs here.

### Chain P — Strict vs non-strict pointer-assignment compatibility

**Status: ACTIVE — UNTRACED.**
**Hop files:** clang/lib/Sema/BSC/SemaBSCSafeZone.cpp (DoPointerTypesSatisfyAssignmentConstraints, DoPointerTypesSatisfyAssignmentConstraintsStrict, DoPointerTypesSatisfyAssignmentConstraintsImpl :482).
**Reopen-if:** a commit touches either DoPointerTypesSatisfyAssignmentConstraints variant.

There are TWO pointer-assignment-compat predicates (strict + non-strict) over the shared *Impl (:482, F76's root). WHICH callers use strict vs non-strict, and do they agree? A context that should use the strict check but calls the non-strict one (or vice versa) = a soundness gap. Map callers of each via who_calls.sh; find a context where the laxer one is used where the strict was required.

### Chain Q — Borrow-pointer comparison + reborrow checks

**Status: ACTIVE — UNTRACED.**
**Hop files:** clang/lib/Sema/BSC/SemaBSCOwnership.cpp (CheckBorrowQualTypeCompare, CheckNeedReborrowPointerType, CheckNeedCastQualifiedType).
**Reopen-if:** a commit touches the borrow-compare or reborrow-pointer check.

Borrow-pointer COMPARISON (==,!=,<) and REBORROW (&_Mut *p) have their own Sema checks, unchained. Does comparing/reborrowing a borrow pointer ever launder a qualifier or lifetime? who_calls CheckBorrowQualTypeCompare / CheckNeedReborrowPointerType; probe == on owned/borrow mixes, reborrow through nested types.

### Chain R — Safe-zone uninit-at-declaration rules

**Status: ACTIVE — UNTRACED.**
**Hop files:** clang/lib/Sema/BSC/SemaBSCSafeZone.cpp (CanBeUninitializedInSafeZone), the must-be-initialized-before-use rule.
**Reopen-if:** a commit touches CanBeUninitializedInSafeZone or the safe-zone init rule.

CanBeUninitializedInSafeZone decides which types may be left uninitialized in _Safe (basic types yes; pointers/owned/borrow no; structs-with-pointer-fields need complete init). A type wrongly classified as 'can be uninit' = a _Safe value used uninitialized (FN). Tabulate (type x can-be-uninit) vs the documented rule; probe a pointer-bearing type the predicate wrongly allows uninit.


### Chain S — Safe-zone builtin-type conversion matrix

**Status: ACTIVE — high priority (F71 was ONE wrong cell).**
**Hop files:** clang/lib/Sema/BSC/SemaBSCSafeZone.cpp (IsSafeBuiltinTypeConversion :89, IsSafeConstantValueConversion :152, IsSafeConversion).
**Reopen-if:** a commit touches the safe-zone scalar conversion matrix.

IsSafeBuiltinTypeConversion is a TYPE-PAIR MATRIX deciding which implicit scalar conversions are safe in _Safe. F71 found ONE wrong cell ([LongLong][ULong] ulong->longlong sign-flip accepted). Matrices usually have MORE wrong cells. AUDIT systematically: for every (src,dst) scalar pair, does the matrix's verdict match soundness? Probe sign-changing (unsigned<->signed same width), narrowing (wider->narrower), float<->int, the compile-time-constant exemption (IsSafeConstantValueConversion). Find a DISTINCT wrong cell (different type pair than F71).


### Chain T — Homogeneous redecl param-diff (HasDiff* family)

**Status: ACTIVE — UNTRACED.**
**Hop files:** clang/lib/AST/BSC/TypeBSC.cpp + clang/lib/Sema/BSC/SemaDeclBSC.cpp (HasDiffBorrowOrOwnedParamsTypeAtBothFunction, HasDiffNullabilityParamsTypeAtBothFunction, HasDiffBorrowOrOwnedQualifiers), called from MergeFunctionDecl.
**Reopen-if:** a commit touches the homogeneous-redecl param/qualifier-diff checks.

The HOMOGENEOUS redecl (two decls of the SAME safety level) param/return qualifier-diff. F77 hit the HETEROGENEOUS path (AreParamTypesCompatible); this is the homogeneous HasDiff* family (F53/F56/F57). Does the homogeneous param-diff have the same nesting gaps (fnptr-param, deeper pointer, arrayelem) that F77 has? Tabulate (HasDiff predicate x dimension x nesting); probe a homogeneous redecl pair with a nested qualifier diff that should be rejected.


### Chain U — BSCOwnership parallel check-families (checkS/checkOPS/checkBOP × Assign/FieldAssign/FieldUse/Use/DerefAssign)

**Status: ACTIVE — UNTRACED.**
**Hop files:** clang/lib/Analysis/BSC/BSCOwnership.cpp.
**Reopen-if:** a commit touches any check{S,OPS,BOP}{Assign,FieldAssign,FieldUse,Use,DerefAssign} function.

C1 sibling-asymmetry, the session's dominant defect shape. Three parallel ownership-check families gated by pointer category — S (raw/standard ptr), OPS (_Owned ptr), BOP (_Borrow ptr) — each with 5 operation handlers. F44/F45/F61/F67 all live in the checkS* row. Audit: does every fix/case in checkS* have its mirror in checkOPS* and checkBOP* (and vice versa)? A handler present in one family but stubbed/missing in a sibling = the gap. who_calls each; tabulate (family × op × case) accept/reject; the asymmetric cell is the candidate. Confirm any memory-mismatch via vg_probe.


### Chain V — Borrow-side nested-detection mirror (CheckBorrowOrIndirectBorrowType / CheckNestedBorrowType / hasBorrowFields)

**Status: ACTIVE — UNTRACED.**
**Hop files:** clang/lib/Sema/BSC/SemaBSCOwnership.cpp clang/lib/AST/BSC/TypeBSC.cpp.
**Reopen-if:** a commit touches CheckBorrowOrIndirectBorrowType / CheckNestedBorrowType / CheckBorrowFunctionType / hasBorrowFields.

The _Borrow-side MIRROR of Chain O (where F80 lives). F80 = CheckOwnedOrIndirectOwnedType (:122) gated its INDIRECT arm on the shallow isMoveSemanticType (misses int*_Owned*-in-struct). Question: does CheckBorrowOrIndirectBorrowType (:969) gate its indirect arm on a correspondingly shallow predicate, and does hasBorrowFields (TypeBSC.cpp:81/92/456) walk the pointer-pointee chain or only the outer level (the F77/F80 outer-only shape)? Diff the _Borrow predicate against its _Owned twin line-by-line; any case the _Owned side checks that the _Borrow side skips (or vice versa) = the gap. _Borrow escape via a nested/indirect borrow that outlives its referent = dangling = the high-value find.


### Chain W — Init-analysis dest-Place projection coverage (transferStatement Assign)

**Status: TRACED 2026-05-30 → F83.**
**Hop files:** clang/lib/Analysis/BSC/BSCIRInitAnalysis.cpp.
**Reopen-if:** a commit touches InitAnalysis::transferStatement Assign-case / getFieldPath / the ensure_init branch.

F83: a dest Place ending in an Index projection (`s.p[i]=v`) skips the uninit-use check on the loaded base pointer `s.p`; getFieldPath returns None (path not pure-Field) and the Deref-first branch needs Projections[0]==Deref. Audit: does EVERY dest-projection shape {Field, Index, Deref, Field+Index, Index+Field, Deref+Index} feed the loaded base into the same uninit-use check the READ side (checkOperand) uses? F83 = the [Field,Index] cell. Remaining cells (deeper index chains, index-then-field) UNTRACED — likely same gap, one fix.


### Chain X — Nullability path-fact invalidation on base reassignment (DerefPath vs FieldPath)

**Status: TRACED 2026-05-30 → F84.**
**Hop files:** clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp.
**Reopen-if:** a commit touches VisitBinaryOperator reassignment handling / InvalidateDerefStatusForVar / adds InvalidateFieldStatusForVar.

F84: on a base-VarDecl reassignment `s=other()`, VisitBinaryOperator (:613-631) invalidates the DerefPath map (InvalidateDerefStatusForVar) but NOT the FieldPath map (CurrStatusFP) — stale `s->f` narrowing survives -> null deref. Audit: does EVERY path-sensitive narrowing map get invalidated on the events that should clear it (reassignment, address-taken, call-may-mutate, field-write-through-alias)? F84 = FieldPath-on-reassignment. Check: FieldPath on field-write `s->f = ...` (:642-645 updates exact key only, never invalidates deeper/sibling FP); DPVD on field-write; any third narrowing map.


### Chain Y — Init-analysis terminator-operand coverage (InitAnalysis::run terminator switch)

**Status: TRACED 2026-05-30 → F88 + F90.**
**Hop files:** clang/lib/Analysis/BSC/BSCIRInitAnalysis.cpp.
**Reopen-if:** a commit touches InitAnalysis::run terminator-check loop / transferTerminator.

InitAnalysis::run (:1513-1543) use-checks operands for only 2 terminator kinds: Call (Args only — F88 found Callee skipped) and Return. F90 found SwitchInt has NO case at all (discriminant unchecked). Remaining UNTRACED terminator kinds: Goto, Drop (Drop.Dropped Place), Unreachable, the conditional/indirect-goto. Audit each for an unchecked operand that reads uninit. C2/C3.


### Chain Z — form-based visitors omit comma / pointer-arith opcodes (F91/F92/F93 cross-analyzer pattern)

**Status: SATURATED (2026-06-04, cycle 10).** All hops audited: ownership/nullability/borrow = F91/F92/F93 (filed); BSCIRInitAnalysis IMMUNE (lowering flattens comma/arith); RewriteBSC emits valid C. Reopen only if a NEW AST-walking analyzer/visitor is added.
**Hop files:** BSCOwnership.cpp VisitCStyleCastExpr+VisitBinaryOperator; BSCNullabilityCheck.cpp getExprPathNullability; BSCBorrowChecker.cpp ActionExtract::VisitBinaryOperator + DefUse::VisitBinaryOperator; BSCIRInitAnalysis.cpp + BSCIRBuilder.cpp (UNAUDITED).
**Reopen-if:** any analyzer adds/edits a BinaryOperator/CStyleCast/Unary visitor that switches on opcode or expr-shape.

Per-analyzer expr visitors that enumerate opcodes/shapes drop BO_Comma and pointer-arith (BO_Add/BO_Sub). CONFIRMED: F91 (ownership void-cast else-branch, CallExpr/comma), F92 (nullability getExprPathNullability BinaryOperator omits arith), F93 (borrow ActionExtract::VisitBinaryOperator omits BO_Comma). UNAUDITED hops: BSCIRInitAnalysis expr/operand lowering, BSCIRBuilder lowerExpr, RewriteBSC. Each analyzer = distinct root cause/fix (not folds). Check whether comma is lowered away before IR-level analyses see it. **UPDATE 2026-06-04 (cycle 9):** BSCIRInitAnalysis hop PROBED-SOUND — comma/arith are flattened by lowerExpr before the init analysis sees them, so IR-level analyses are IMMUNE. Blast radius bounded to AST-walking analyzers (F91/F92/F93 confirmed). Remaining unaudited AST-walking hop: RewriteBSC.


### Chain CE1 — SafeExpr transparent-wrapper coverage across CodeGen emission emitters (Scalar/Agg/Complex/ConstRValue/ConstLValue)

**Status: TRACED.**
**Hop files:** clang/lib/CodeGen/CGExprScalar.cpp clang/lib/CodeGen/CGExprAgg.cpp clang/lib/CodeGen/CGExprComplex.cpp clang/lib/CodeGen/CGExprConstant.cpp.
**Reopen-if:** any of the 5 emission emitters changes its Visit dispatch, or a new StmtVisitor emitter is added.

F60(Agg)+F63(Complex) FIXED by a9deb1b; G09 = ConstExprEmitter+ConstantLValueEmitter (both CGExprConstant.cpp) still missing VisitSafeExpr. Scalar always had it. Audit complete 2026-06-17 — no 4th emission gap.


### Chain AA — isNullExpr null-classification consumed divergently by ownership vs nullability

**Status: ACTIVE.**
**Hop files:** clang/lib/AST/BSC/ExprBSC.cpp clang/lib/Analysis/BSC/BSCOwnership.cpp clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp.
**Reopen-if:** isNullExpr dyn_cast ladder, or ownership VisitDeclStmt/VisitBinaryOperator null-handling, or getExprPathNullability changes.

Ownership detects 'is this value null' via the SYNTACTIC Expr::isNullExpr (ExprBSC.cpp dyn_cast ladder: IntegerLiteral/CStyleCast/ImplicitCast/ParenExpr/CallExpr-raw-builtins/getIntegerConstantExpr — NO DeclRefExpr arm), consumed at BSCOwnership VisitBinaryOperator:2201 (assign-null) + VisitDeclStmt:2377 (init-null). Nullability classifies the SAME expr via the FLOW-SENSITIVE getExprPathNullability:318 (consults CurrStatusVD + declared nullability AND calls isNullExpr). PEER DISAGREEMENT: a value nullability KNOWS is null but isNullExpr returns false -> ownership treats a provably-null _Owned _Nullable as still-owned. EXEMPLAR F108. RE-WALK: enumerate null-valued forms nullability=Nullable that isNullExpr misses (DeclRefExpr/ConditionalOperator/flow-narrowed); per form build an _Owned _Nullable assign/init/copy, check ownership FP-leak OR the dual setToNull-on-live-owned FN.


### Chain AB — getMemberFullField member-path extraction consumed by setToAllMoved vs setToNull vs subscript/member visitors (ownership-side path consistency)

**Status: ACTIVE.**
**Hop files:** clang/lib/Analysis/BSC/BSCOwnership.cpp.
**Reopen-if:** getMemberFullField, setToAllMoved, setToNull, VisitArraySubscriptExpr, or VisitMemberExpr changes.

getMemberFullField (BSCOwnership.cpp) extracts the field-path string from a MemberExpr; consumed by setToAllMoved:769 (move), setToNull:835 (null), VisitArraySubscriptExpr:2091, VisitMemberExpr:2106. If a SUBSCRIPT-containing member path (arr[i].f, s.arr[i].f) is collapsed/dropped inconsistently across these consumers, a conditional move or null of an owned field under a subscript is mis-tracked -> double-free FN (move not recorded at the use-check) or FP. ANALOG: F109 (borrow-side ActionExtract subscript path collapse, UsesArraySubscriptNotation display-only). RE-WALK: build arr[i].v owned-field move-then-consume + arr[i].v null-init + arr[i].v double-consume and verify ownership tracks the path identically across setToAllMoved/setToNull/use-check; a mismatch = the find. NOTE array-of-_Owned-field-struct may be decl-rejected (round 403 OOS generic only) — use a PLAIN struct array element holding the owned field if expressible, else fold.


### Chain AC — ActionExtract Action-kind classification (Assign/Init/Use/Noop) consistency in the borrow checker

**Status: ACTIVE.**
**Hop files:** clang/lib/Analysis/BSC/BSCBorrowChecker.cpp.
**Reopen-if:** ActionExtract::VisitBinAssign Assign/Init downgrade gate (:507), VisitCallExpr (:513), or the Action Init/Assign loan-kill handlers change.

ActionExtract assigns each statement an Action::Kind (Assign/Init/Use/Noop); the loan-kill semantics differ (Assign kills the dest's prior loan via LoansKilledByWriteTo; Init does NOT). VisitBinAssign downgrades Assign→Init when RNL.isInvalid()||RNR.isInvalid()||Sources.empty()||!IsTrackedType(BO->getType()) (:507-509). A misclassification could leave a stale loan (FP) or skip a kill. PROBED @34883aa1:  RHS (RNR invalid→Init) does NOT cause a stale-loan FP — liveness kills b's old loan because b is reassigned (backstop, cf F112). RE-WALK other RNR-invalid borrow-assign RHS forms (failed-region borrow exprs, complex reborrow that yields no Source) where the dest is STILL live afterward (so liveness does NOT kill the old loan) → then the Init-downgrade's skipped Assign-kill is exposed (the F75/F109 sibling: loan-kill not applied at a holder reassign). Distinct from F109 (subscript path collapse in the SAME VisitBinAssign) by mechanism (action-kind downgrade vs path collapse).


### Chain AD — owned/borrow placement gates + has{Owned,Borrow}Fields array-field recursion

**Status: PROBED-F124.**
**Hop files:** clang/lib/AST/BSC/TypeBSC.cpp clang/lib/Sema/SemaDecl.cpp clang/lib/Sema/SemaType.cpp.
**Reopen-if:** TypeBSC.cpp has*Fields or the gate call-sites change.

Gate-pair audit: var(8556/57)+union(18723/24) have both gates; array decl(5202) borrow-gate missing=F81. Predicate hasBorrowFields(477) array-field-blind=F124 (owned twin sound). Fix hasBorrowFields array-unwrap subsumes both.


### Chain AE — isArrayElemQualified array-elem qualifier across borrow-checker (DefUse/ActionExtract) + Sema safe-zone (AreBSCPointerQualifiersCompatible/IsSafePointerConversion)

**Status: ACTIVE — UNTRACED.**
**Hop files:** clang/lib/Analysis/BSC/BSCBorrowChecker.cpp clang/lib/Sema/BSC/SemaBSCSafeZone.cpp.
**Reopen-if:** isArrayElemQualified consumers or array-elem conversion/loan handling changes.

TODO: per-hop invariants + the weakest-hop differential. Use scripts/who_calls.sh / what_calls.sh to map the hops.


### Chain AF — IsCastFromVoidPointer void->typed owned RECOVERY (__take_from_raw round-trip) consumed at 4 ownership TransferFunctions sites

**Status: ACTIVE — UNTRACED.**
**Hop files:** clang/lib/Analysis/BSC/BSCOwnership.cpp.
**Reopen-if:** IsCastFromVoidPointer definition or its 4 consumer sites change.

TODO: per-hop invariants + the weakest-hop differential. Use scripts/who_calls.sh / what_calls.sh to map the hops.


### Chain AG — hasOwnedFields owned-field detector (Type::hasOwnedFields TypeBSC.cpp:72) array-blindness — F81-twin; consumed at 5 ownership sites + Sema CheckBSCQualTypeAssignment

**Status: ACTIVE — UNTRACED.**
**Hop files:** clang/lib/AST/BSC/TypeBSC.cpp clang/lib/Analysis/BSC/BSCOwnership.cpp.
**Reopen-if:** Type::hasOwnedFields array recursion, or owned-array decl gate (CheckOwnedOrIndirectOwnedType) changes.

TODO: per-hop invariants + the weakest-hop differential. Use scripts/who_calls.sh / what_calls.sh to map the hops.


### Chain AH — Nullability variance checks don't recurse into fnptr pointees where owned variance checks do

**Status: PROBED.**
**Hop files:** clang/lib/Sema/BSC/SemaDeclBSC.cpp (HasDiffNullabilityQualifiers:80, CheckNullabilityQualTypeAssignment:170); clang/lib/Sema/SemaDecl.cpp:4447; clang/lib/Sema/BSC/SemaBSCSafeZone.cpp (owned fnptr variance).
**Reopen-if:** any new nullability variance/compat check is added, or SemaDeclBSC.cpp:80/170 or SemaDecl.cpp:4440-4452 change.

Owned variance checks (HasDiffBorrorOrOwnedQualifiers

**AUDIT COMPLETE (2026-07-01).** Three nullability-variance predicates, all sharing the missing-fnptr-pointee-recursion gap that the owned twins DON'T have:
1. `HasDiffNullabilityQualifiers` (SemaDeclBSC.cpp:80-85) — HOMOGENEOUS redecl gate (SemaDecl.cpp:4447). Top-level `getNullability()` only, no fnptr recursion. → **F125 (FILED, HIGH)**. Owned twin `HasDiffBorrorOrOwnedQualifiers`:100-111 recurses.
2. `CheckNullabilityQualTypeAssignment` (SemaDeclBSC.cpp:170-203) — ASSIGNMENT/arg/return. Recurses into pointer pointees (:197 isPointerType) but a function type is not a pointer → fnptr param/return nullability never checked. → **F126 (FILED, HIGH)**. Owned twin = SemaBSCSafeZone fnptr variance (rejects).
3. `AreParamTypesCompatible` (TypeBSC.cpp:256) — HETEROGENEOUS `_Safe`/`_Unsafe` redecl. `stripOuterNullability` then checks only owned/borrow; fnptr recursion (`areFunctionTypesCompatibleForHeterogeneousRedecl`) only fires on a safety-zone mismatch and the nullability was already stripped. → **F103 (user's active area — DO NOT FILE)**.
Common fix shape: every nullability variance/compat predicate must recurse into FunctionProtoType pointee params (contravariant) + return (covariant), mirroring the owned handling. REOPEN if a 4th nullability variance predicate is added or any of the three change., SemaBSCSafeZone) recurse into fnptr pointee params/return; the nullability twins (HasDiffNullabilityQualifiers redecl=F125, CheckNullabilityQualTypeAssignment assignment=F126) do NOT. Audit EVERY nullability variance/compat predicate for the same missing fnptr-pointee recursion.


### Chain AI — Nullability flow-narrow must be invalidated on EVERY state-changing op, not just direct assignment

**Status: PROBED.**
**Hop files:** clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp (VisitCallExpr:659, InvalidateDerefStatusForVar:588, VisitBinaryOperator assign).
**Reopen-if:** BSCNullabilityCheck.cpp VisitCallExpr / InvalidateDerefStatusForVar / assign handling changes.

Direct reassign p=q invalidates the narrow (sound); address-escape f(&p) does NOT **PROBED 2026-07-01:** call-arg escape `f(&p)` = **F127 (FILED, HIGH, runtime SIGSEGV)**; local-alias write `pp=&p; *pp=null; *p` = FOLDED-F127 (same root, fix belongs at address-of site covering both). Root: VisitCallExpr:659 + address-of handling never invalidate a var's narrow (CurrStatusVD) on `&var`. **AUDIT COMPLETE 2026-07-01:** loop-back-edge modification → PROBED-SOUND (dataflow fixpoint propagates reassign-nullable to loop-top use); struct-field escape `s.pp=&p` folds into F127 (same address-escape root as the confirmed local-alias case). RESULT: modeled modifications (direct reassign, loop merge) sound; unmodeled address-escape = F127 (single root, fix at the address-of site). REOPEN if BSCNullabilityCheck VisitCallExpr/address-of handling changes. (F127, FILED). Audit every op that can change a narrowed var: call-arg address-escape (F127), local-alias write (*pp=null where pp=&p), loop back-edge modification. Owned analyzer tracks address-escape (GetAddr); nullability must too.

<!-- CHAIN-ENTRIES-END: scripts/add_chain.sh inserts new "### Chain X" stubs immediately ABOVE this line. Do not move. -->

---

## Method

For every active chain entry above:

1. **Map the chain**: list every hop (caller → callee, or Transform → Restore).
   Source paths with line numbers. This is the "node list".
2. **Per-hop predicate**: write the invariant the hop *should* satisfy
   (what AST forms it handles, what wrappers it strips, what variance
   dimensions it checks).
3. **Rank hops**: any hop whose invariant looks weaker than its peers is
   a top candidate. Probe with a minimal differential — call the chain
   on a shape that should trip the weaker hop.
4. **Log outcome** in `_probed.md` as for any other probe.

A chain audit is "done" when every hop has a probed-or-inspected entry.

---

## Updating this file

Add new chains as they're discovered. Mark active vs idle. When a chain
yields its filed exemplar, leave the entry so future sessions know the
shape.

<!-- REOPEN-EVENT 2026-06-29 @34883aa1 (range 28656aa9..HEAD): check_chain_reopen.sh flags 8 chains REOPENED -->
## REOPENED 2026-06-29 @34883aa1 (compiler advanced 28656aa9→34883aa1; hop files changed)
8 chains reopened — re-walk surface (previously SATURATED @ 28656aa9/28656aa9):
- **A** (CheckTemporaryVarMemoryLeak callers) — touched SemaBSCOwnership/SemaExpr/SemaStmt/ParseExpr
- **B** (Prologue/Epilogue/RegionInference) — touched SemaDeclBSC/BSCBorrowChecker
- **C** (BinAssign × 4 analyzers) — touched ALL 4 analyzer files; nullability hop re-walked SOUND (above), other hops pending
- **E** (CFG element granularity) — touched SemaDeclBSC/BSCBorrowChecker
- **F** (getExprPathNullability switch) — touched BSCNullabilityCheck; F92 (BO_Add/BO_Sub) freshly from here, re-audit for siblings
- **I** (array-decay-to-_Borrow) **UNTRACED** — touched SemaBSCSafeZone/SemaExpr
- **K** (two-CFG-build asymmetry) — touched SemaDeclBSC
- **L** (mixed-mode overload + fnptr assign) **UNTRACED** — touched SemaBSCSafeZone
