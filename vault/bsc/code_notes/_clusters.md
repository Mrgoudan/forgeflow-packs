# Fix-Opportunity Clusters

A view of the campaign's findings grouped by **fix opportunity** — which
bugs naturally share a PR, a code-site family, or a fix template. This
is orthogonal to the existing cross-cutting views:

- `_playbook.md` — per **defect class** (C1–C8). What *kind* of bug.
- `bug_log.md` — per **individual bug** (F01–F68). Specific repro + fix.
- `_chains.md` — per **call-chain audit surface**. Where to *look* for more.
- **this file** — per **fix opportunity**. Which bugs *fix together*.

A cluster crosses class boundaries when the underlying invariant is the
same even if the playbook taxonomy splits them. The cluster's PR title
should let a reviewer see the family at a glance.

Each cluster lists why the included bugs belong together AND why nearby
cousins do NOT — the F49→F28 incident and the F62-vs-F65 nuance both
show that "looks similar" is not the same as "fixes together." Document
why-not as carefully as why.

---

## Cluster A — Compound-assign vs simple-assign in nullability tracking

**Bugs**: F18, F48, F50

**Shared invariant**: compound assignment (`p += k`, `p -= k`, …) is
**not** semantically equivalent to simple assignment (`p = q`) for
nullability tracking. Three independent predicates in the nullability
subsystem treat them as if they were.

**Per-site detail**:
- **F18** — `BSCNullabilityCheck.cpp:371` (`getExprPathNullability`
  switch) — missing `BO_Add`/`BO_Sub` cases. Pointer arithmetic
  launders Nullable into Unspecified.
- **F48** — `BSCNullabilityCheck.cpp:608-619` (`VisitBinaryOperator`
  state-update) — compound-assign overwrites `CurrStatusVD[p]` with
  the integer-RHS's Unspecified instead of preserving prior Nullable.
- **F50** — `BSCNullCheckInfo.cpp:170` (`extractDistinguishedTrackablePtr`)
  — `isAssignmentOp()` too broad; accepts compound-assigns as
  null-check conditions and narrows `p` to NonNull in the true branch.

**Fix template (per-site)**: distinguish `BO_Assign` from compound-assign
opcodes; either enumerate the cases explicitly or use a tighter
predicate. Each site has a different one-line fix.

**Suggested PR title**: `[nullability] Compound-assign treated as simple-assign at three independent sites (F18, F48, F50)`

**Why grouped**: shared invariant; a reviewer reading any one in
isolation would want context on the other two; the fix template is
nearly identical at each site even though the lines edited differ.

**Why other apparent cousins are NOT in this cluster**:
- F11 (BO_Comma not in DefUse::VisitBinaryOperator) — same "switch on
  binop misses a case" *pattern* but BO_Comma is not in the
  assign-family; the semantic mistake is unrelated. Different cluster.

---

## Cluster B — Recursive type predicate stops at outer level

**Bugs**: F41, F53, F56, F57, F68

**Shared invariant**: a predicate that compares qualifiers between two
pointer types must recurse through every type constructor that can
carry the qualifier — not just `PointerType`. The five sites each stop
at the outer level, missing qualifiers buried inside `FunctionProtoType`
parameters and return types.

**Per-site detail**:
- **F41** — `CheckOwnedFunctionPointerType` (`SemaBSCOwnership.cpp:440-479`).
  Fnptr assignment with nested `_Owned`. `isOwnedQualified()` at outer
  level only.
- **F53** — `HasDiffBorrorOrOwnedQualifiers` (`SemaDeclBSC.cpp:85-101`).
  Function redecl with mismatched `_Borrow`/`_Owned` inside fnptr-param
  silently merged. Recursion arm for `FunctionProtoType` is missing
  next to the existing `PointerType` arm.
- **F56** — `HasDiffNullabilityQualifiers` (`SemaDeclBSC.cpp:78-83`).
  Same shape as F53, nullability dimension.
- **F57** — `AreOwnedBorrowQualifiersCompatible` (`TypeBSC.cpp:154-181`).
  Heterogeneous safe/unsafe redecl with same recursion gap.
- **F68** — `IsSafeFunctionPointerTypeCast`
  (`SemaBSCSafeZone.cpp:486-579`). Safe-zone dimension on the
  implicit-fnptr-cast path.

**Fix template**: at each predicate, add a `FunctionProtoType` arm
after the existing `PointerType` arm:
```cpp
if (const auto *FT = T->getAs<FunctionProtoType>()) {
  // recurse on FT->getReturnType() and each FT->getParamType(i)
}
```
Whether the qualifier dimension is `_Owned`/`_Borrow`, `_Nullable`,
or safe-zone changes the inner check; the outer recursion structure
is identical.

**Suggested PR title**: `[type-predicates] Only-outer-level recursion at five sites — Owned / Borrow / Nullable / Safe / Hetero-redecl`

**Why grouped**: identical structural fix; five different qualifier
dimensions but one PR convinces the reviewer they understand the
pattern once and apply it five times.

**Why other apparent cousins are NOT in this cluster**:
- F66 (`CheckNullabilityQualTypeAssignment`'s `LHSNull && RHSNull`
  short-circuit). Surface-similar (also a nullability assignment
  gate), but the bug is the optional-pair short-circuit defaulting
  the missing side, not a recursion-depth gap. Different fix surface.
- F29 (`CheckBSCFunctionPointerType` missing nullability dimension
  entirely). Different defect: a whole dimension absent rather than a
  recursion stopping short. Could plausibly go in the same PR, but
  the fix is "add the dimension," not "extend the recursion."

---

## Cluster C — Codegen / Rewriter emitter missing `VisitSafeExpr` override

**Bugs**: F58, F60, F63

**Shared invariant**: BSC's `SafeExpr` AST node (from `_Safe(...)` /
`_Unsafe(...)`) is a transparent wrapper. Every codegen visitor that
can receive a typed sub-expression must recurse through it. Three
emitters in CodeGen — and a related builtin handler — do not.

**Per-site detail**:
- **F58** — `CGBuiltin.cpp:2795-2799` (`Builtin::BI__assume_initialized`
  handler). Returns no-op `RValue` without emitting argument; side
  effects in the argument silently dropped.
- **F60** — `CGExprAgg.cpp:80-200` (`AggExprEmitter`). No
  `VisitSafeExpr`; aggregate-typed `_Safe(<aggregate-expr>)` falls
  to `ErrorUnsupported`.
- **F63** — `CGExprComplex.cpp:45-401` (`ComplexExprEmitter`). Same
  shape as F60; `_Complex`-typed `_Safe(...)` falls to
  `ErrorUnsupported`.

**Fix template**: each emitter gets one method:
```cpp
ResultTy VisitSafeExpr(SafeExpr *E) { return Visit(E->getSubExpr()); }
```
F58 is slightly different — it's a builtin handler, not a
`StmtVisitor`-style override — but the conceptual gap (emitter
silently drops a child) is the same and the fix (call
`EmitIgnoredExpr` on the argument) is one line.

**Suggested PR title**: `[codegen] SafeExpr transparently dropped by aggregate / complex emitters and __assume_initialized handler`

**Why grouped**: three emitters, three one-line fixes, single
conceptual story. Bundling lets the reviewer audit all three in one
mental pass.

**Why other apparent cousins are NOT in this cluster**:
- F62, F65 (Sema-side SafeExpr-strip). Different layer (Sema vs
  CodeGen). The Sema sites use `IgnoreParenCasts` helpers from
  `IgnoreExpr.h`; the CodeGen sites use `StmtVisitor` dispatch. The
  cross-cutting fix at `IgnoreExpr.h` (see Cluster D) does NOT
  reach CodeGen because codegen visitors don't consult that helper.

---

## Cluster D — `SafeExpr` not stripped by `IgnoreParenCasts` in Sema

**Bugs**: F62, F65 + the F62-folds noted in `_probed.md` (variants at
`CheckTemporaryVarMemoryLeak`, `getMemberFullField`, etc., logged
but not filed separately).

**Shared invariant**: Sema-level predicates that resolve "what is the
real expression we're checking?" use `IgnoreParenCasts()` /
`IgnoreParenImpCasts()` from `clang/include/clang/AST/IgnoreExpr.h`.
The strip list there does not include BSC's `SafeExpr`. So wrapping
any expression in `_Safe(...)` / `_Unsafe(...)` defeats every
Sema-level predicate that relies on that helper family.

**Per-site detail**:
- **F62** — `CheckMoveVarMemoryLeak` (`SemaBSCOwnership.cpp:547-559`).
  Soundness FN — silent runtime double-free.
- **F65** — narrowing helpers in `BSCNullabilityCheck.cpp:164-215`
  and `BSCNullCheckInfo.cpp:100-128`. Precision FP — null-check
  through `_Safe(p)` loses narrowing.
- **F62 folds** (in `_probed.md`, not filed) —
  `CheckTemporaryVarMemoryLeak` (F14/F47 territory),
  `getMemberFullField` (F30/F46 territory), `BuildUnaryOp` UO_AddrMut
  string-lit guard (F36 area), `CheckBorrowQualTypeCStyleCast` (F27
  area). Each fires when the test program wraps in `_Safe(...)`.

**Fix template**: **single one-line edit** to
`clang/include/clang/AST/IgnoreExpr.h` adding `SafeExpr` to the
strip family. Closes every site at once.

**Alternative fix template**: per-call-site
`if (auto *SE = dyn_cast<SafeExpr>(E)) E = SE->getSubExpr();` before
each affected predicate's `IgnoreParenCasts`. Many call sites; same
effect.

**Suggested PR title**: `[ast] Add SafeExpr to IgnoreParenCasts strip list — closes ownership and nullability sites at once`

**Why grouped**: this is the campaign's **highest-leverage** single
patch. One line of code, multiple soundness AND precision wins.

**Why other apparent cousins are NOT in this cluster**:
- F60, F63, F58 (codegen-side SafeExpr drops). Different layer; the
  `IgnoreExpr.h` fix does not reach codegen visitors. See Cluster C.

---

## Cluster E — Visit/switch dispatch table missing an AST kind (heterogeneous)

**Bugs**: F09, F11, F47, F52, F54, F59 (and arguably F18 too — but F18
fits better in Cluster A).

**Shared pattern**: each is "a dispatch table omits a specific
Stmt/Expr/Decl kind, falling to default-no-op." The pattern is
uniform; the *fix sites* are scattered across files and analyzers.

**Why we do NOT recommend bundling this as one PR**: a reviewer would
have to context-switch per item (different analyzer, different
dispatch table, different reason for the kind being missing). The
diff would be six unrelated additions.

**Recommended PR shape**: file per-bug, or sub-cluster by file:
- F11 + F18 — `BSCBorrowChecker.cpp` / `BSCNullabilityCheck.cpp` opcode
  switches missing BO_Comma / BO_Add. (Caveat: F18 fits Cluster A; if
  bundled there, this becomes F11 singleton.)
- F47, F52 — `SemaBSCOwnership.cpp` / `BSCIRBuilder.cpp` missing
  CompoundLiteralExpr / AttributedStmt. Different files; not natural
  to bundle.
- F54, F59 — both in `clang/lib/Frontend/Rewrite/RewriteBSC.cpp` (and
  `WalkerBSC.h`); could bundle as a "rewriter coverage gaps" PR if
  desired.
- F09 — already filed at IJO88R as a historical issue; its fix is
  invasive (rewriter hoisting redesign).

**Suggested partial bundle**: `[rewriter] Decl::StaticAssert and UnaryExprOrTypeTraitExpr coverage gaps (F54, F59)`

---

## Cluster F — Per-field state container collapses at a transition (DESIGN DECISIONS)

**Bugs**: F26, F44, F45, F61

**Why not a bundle**: each is a different *semantic* choice about what
state should propagate through a specific transition. F26 fixes a merge
operator; F44 fixes a `&_Mut` collapse direction; F45 fixes stale-state
survival; F61 fixes struct-copy state mirroring. Each needs a
deliberate design decision (which way should the lattice meet? what's
the conservative position?) that the maintainer should make.

**Recommended handling**: triage to maintainer with the existing bug
reports. Do NOT bundle.

---

## Singletons that don't cluster (or have lone-cousin partners)

- **F14, F22** — `CheckTemporaryVarMemoryLeak` family. F14 is a
  paren-strip omission, F22 is a missing call site. Different bugs,
  different fixes; could bundle as a 2-bug "leak-check robustness" PR
  but small payoff.
- **F30, F46** — both `getMemberFullField` paren-strip. Could bundle.
- **F19** — field-assign-then-read uninit at `checkSFieldAssign`.
  Standalone.
- **F23, F35** — null-init / forward-goto. Each standalone.
- **F33, F34, F36, F37, F38, F39, F40, F42, F43, F44** — each
  standalone or in Cluster F (above).
- **F64** — pointer-to-owned-pointer deref move-tracking missing.
  Standalone HIGH; fix surface is extending `IsTrackedType` plus the
  UO_Deref transfer.
- **F66** — `CheckNullabilityQualTypeAssignment` optional-pair
  short-circuit. Standalone HIGH; close-cousin to Cluster B but
  different bug shape.
- **F67** — `FindNonnull` / `CheckInit` conflate `IncompleteArrayType`
  with `ConstantArrayType`. Standalone MED.

---

## LOW-severity, not filed and not bundled

F01, F06, F07, F13, F15. Doc/UX gaps. Out of scope for automated fix
PR. Triage to maintainer with the existing notes.

---

## Suggested PR sequence (highest leverage first)

| Order | Cluster | Bugs | Why first |
|------:|---------|------|-----------|
| 1 | **D** (SafeExpr in IgnoreExpr.h) | F62, F65, + folds | 1-line patch closes many; demonstrates campaign value |
| 2 | **C** (Codegen VisitSafeExpr) | F58, F60, F63 | 3 small overrides; mechanical |
| 3 | **A** (compound-assign nullability) | F18, F48, F50 | tightest semantic cluster; reviewer can audit in one pass |
| 4 | **B** (only-outer-level recursion) | F41, F53, F56, F57, F68 | five sites, identical structural fix; explains the family pattern |
| 5 | Partial Cluster E (rewriter) | F54, F59 | two small additions in same file |
| 6+ | singletons | as appropriate | one bug per PR; case-by-case |

Stop after the first four or five PRs. The rest are either design
decisions (F44/F45/F61 — needs maintainer) or low-leverage singletons
that the maintainer can prioritise themselves.

---

## Updating this file

A cluster joins when a new finding shares an invariant with an
existing one. Add it; restate why-grouped and why-not against the
nearest non-member. If the cluster's PR ships, mark the cluster
**Filed: <PR-URL>** at the top.

When in doubt about cluster membership: ask "does this fix close the
others?" If yes → same cluster. If maintainer would touch the same
file and same function family → same cluster. If the fix is at a
different file/function with no shared structural change → not in
this cluster, even if the playbook puts them in the same class.
