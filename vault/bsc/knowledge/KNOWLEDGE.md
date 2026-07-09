# BSC compiler development guide

The durable, git-tracked **skill** for working on the BiSheng C *compiler*:
how to code it, the coding standard, and how to test it. This is general
engineering knowledge — bugs are only the *occasion* a lesson was learned; what
we keep is the transferable practice. (Complements the `bsc-bugfix` agent =
rules, and `bisheng-c-skills` = the BSC *language*, not the compiler.)

> See how many times the guide has grown: `./scripts/learnings.sh`.
> Each accepted fix / review reply should sharpen a rule below and log it.

---

## 1. How to code the BSC compiler

**All BSC code is `#if ENABLE_BSC`-guarded and isolated in `BSC/` subdirs** —
edit there; it keeps changes least-invasive and the non-BSC build identical.

| Subsystem | Where | How you extend it |
|-----------|-------|-------------------|
| AST nodes / walkers | `clang/include/clang/AST/BSC/` (`WalkerBSC.h`) | Feature finders (`BSCFeatureFinder`) decide `-rewrite-bsc` routing; add a `Visit<Node>` override; for type-operands (`sizeof`) recurse via `VisitQualType`. |
| Sema checks | `clang/lib/Sema/BSC/` | Predicates over expressions. See through wrappers with `IgnoreParenCastsSafe()` (Paren/casts/`SafeExpr`); for full-expression checks recurse `BO_Comma`'s RHS and `AbstractConditionalOperator`'s arms (covers GNU `?:`). |
| Ownership analysis | `clang/lib/Analysis/BSC/BSCOwnership.cpp` | Three per-var status maps: `OPSStatus` (owned ptr→struct), `SStatus` (struct w/ owned fields), `BOPStatus` (basic owned ptr; deref levels keyed by `*`). Fields are encoded: `.`=member, `*`=deref — **strip the trailing `*` markers before membership tests**. `IsTrackedType` gates BOTH enrolment AND assignment move-consumption — never broaden it blindly (it would treat `&x` as a move). Enrol via `init`→`initOPS/initS/initBOP`. |
| Borrow checker | `clang/lib/Analysis/BSC/BSCBorrowChecker.cpp` | `DefUse` (NLL use/def) + `ActionExtract` (borrow extraction) visitors. `BinaryOperator` dispatches to `VisitBin<Op>` (e.g. `VisitBinAssign`, `VisitBinComma`); opcode-range chains must cover every opcode or add the missing `VisitBin<Op>`. |
| CodeGen | `clang/lib/CodeGen/` | Aggregate/complex/builtin emitters need an explicit `VisitSafeExpr` recursion arm. |
| Rewriter | `clang/lib/Frontend/Rewrite/RewriteBSC.cpp` | Per top-level decl, `BSCFeatureFinder` chooses pretty-print (strips BSC syntax) vs verbatim copy; a missed feature leaks BSC syntax into `.c`. |
| Diagnostics / docs | `clang/include/clang/Basic/BSC/Diagnostic*BSC*.td`; `clang/docs/BSC/{BiShengCLanguageUserManual,bsc-errors}.md` | Add/lookup diagnostic codes; update the manual & error reference when behavior or a diagnostic changes. If a fix *changes what the spec permits* (not just renames a diagnostic), update the relevant User Manual chapter — not just `bsc-errors.md`. If a check is target- or width-dependent, the diagnostic text must say so ("在某些目标上" / "when types have equal width"); a universal-sounding message is wrong when the rejection is conditional. |
| Safe-zone conversion matrix | `clang/lib/Sema/BSC/SemaBSCSafeZone.cpp` | The conversion matrix is LP64-shaped. Never flip a single matrix cell globally to fix a target-specific issue — gate instead on actual type widths (`Ctx.getTypeSize(From) == Ctx.getTypeSize(To)`). Keep the matrix doc-comment strictly consistent with the live runtime guards: if a cell is Y but rejection is actually handled by a later width guard, say so in the comment; divergence between the table and the code makes the area fragile. |

**Recurring change shapes** (the same few keep coming up): a *dispatch table
misses an AST kind* (add the arm) · a *predicate is defeated by a transparent
wrapper* (strip it) · an *encoded field name isn't normalized* (strip markers) ·
a *status isn't recomputed at a transition* · the *rewriter mis-routes*.

**Workflow for a change:** `new_fix.sh` → edit a BSC-only file → add tests →
`verify_fix.sh --strict` → one commit → PR. Before changing behavior, read the
manual; if the spec is ambiguous, stop and ask — don't pick a behavior.

**Rebase on maintainer request:** when a `#fix` says "rebase到最新主干" / "拉取主仓最新代码" /
"bishengc/15.0.4", fetch from the UPSTREAM repo
(`git@gitee.com:bisheng_c_language_dep/llvm-project.git`), NOT `origin` (the fork's
base is stale). Then `git rebase upstream/bishengc/15.0.4`, resolve any conflicts, and
force-push the branch. Always comment the outcome. This is distinct from an ordinary
code fix — no source files change, only the base commit changes.

---

## 2. Coding standard

- **LLVM coding standard** — match surrounding brace style, naming, abstraction;
  reuse existing helpers (`dyn_cast`, the `Ignore*` family) — never reinvent.
- **`#if ENABLE_BSC` + BSC-only files.** Prefer a BSC variant *beside* the
  generic helper (e.g. `IgnoreParenCastsSafe` next to `IgnoreParenCasts`) over
  editing shared code. Least-invasive; non-BSC build byte-identical.
- **Essentially no comments.** Maintainers strip even "why" comments; default to
  zero. Let the code and `fixes/<unit>/plan.md` carry the reasoning.
- **BSC pointer qualifier goes after `*`:** `T *_Owned`, never `_Owned T*`.
- **Root cause, not band-aid.** Widen the wrong predicate; don't add a downstream
  special-case skip.
- **One PR = one issue = one commit.** Review changes are *amended into* the
  PR's commit, never a second commit.

---

## 3. How to test

- **Tests live in the BSC corpus**, not in autofix:
  `clang/test/BSC/{Positive,Negative,BSCIR}/<area>/<name>.cbs`.
- **BSCIR lowering changes require a dump-IR test** under `clang/test/BSC/BSCIR/`.
  Use `// RUN: %clang -emit-llvm/-emit-bscir ... | FileCheck %s` to assert the emitted
  IR structure. Without this, a BSCIR lowering regression is invisible to the corpus.
- **Negative:** `// RUN: %clang_cc1 -verify %s`, then
  `// expected-error{{substr}}` / `expected-warning{{…}}` / `expected-note{{…}}`
  on the diagnostic's line (several directives per line allowed).
- **Positive:** `// expected-no-diagnostics`; cover valid patterns the fix must
  keep accepting (and a false-positive's now-accepted form).
- **Rewriter:** `// RUN: %clang -rewrite-bsc %s -o %t.c` then
  `// RUN: %clang -c %t.c -o %t.o` — **compiling the rewritten `.c` is the real
  gate** (it fails if BSC syntax leaked) — plus `FileCheck`.
- **Be exhaustive:** enumerate operator/direction/wrapper/nesting/value-category;
  list deliberately-excluded folds with the reason. Reference umbrella `IJP3AP`.
- **Avoid incidental diagnostics:** an unused result trips `-Wunused-value` /
  `-Wunused-variable` — use the value (`return *p;`) or annotate the warning.
- **Gate:** `verify_fix.sh --strict` = compiled **and** umbrella repros match
  expected **and** the full `check-clang-bsc` corpus passes. The lit run needs
  `FileCheck`, `llvm-config`, `count`, `not` built (`ninja` those once).
- **Prove surgical:** stash + rebuild the base and diff — only the targeted
  repro outcome should move; everything else byte-identical.

---

## Lessons log — how this guide grew

One row per learning. `type` = `bug-fix` or `review` (the occasion). The point of
the row is the **rule it sharpened** (in §1/2/3), not the bug. `learnings.sh`
counts this table.

| #  | date       | type    | rule sharpened (§) | from |
|----|------------|---------|--------------------|------|
| 1  | 2026-05-27 | bug-fix | §1 wrapper stripping: add a BSC `*Safe` variant beside the generic strip helper | F62,F65 #834 |
| 2  | 2026-05-28 | bug-fix | §1 CodeGen: emitters need a `VisitSafeExpr` recursion arm | F58,F60,F63 #837 |
| 3  | 2026-05-26 | bug-fix | §1 rewriter: decl switch must cover every top-level decl kind | F54 #841 |
| 4  | 2026-05-28 | bug-fix | §1 analysis: peel `ParenExpr`/`SafeExpr` when resolving a member base | F30,F46 #843 |
| 5  | 2026-05-28 | bug-fix | §1 BSCIR: lower `AttributedStmt`; guard truncated field paths | F52,F55 #844 |
| 6  | 2026-06-01 | bug-fix | §1 ownership: strip the `*` deref marker before moved-field membership | F67 #847 |
| 7  | 2026-06-01 | bug-fix | §1 Sema: full-expression checks must strip wrappers + recurse comma/cond | F14 #848 |
| 8  | 2026-06-02 | bug-fix | §1 walkers: feature finder must walk `sizeof`/`_Alignof` type operands | F59 #849 |
| 9  | 2026-06-02 | bug-fix | §1 borrow: cover every `BinaryOperator` opcode incl. `BO_Comma` | F11 #850 |
| 10 | 2026-06-02 | review  | §1 Sema: match `AbstractConditionalOperator` (covers GNU `x ?: y`), not just `ConditionalOperator` | F14 #848 |
| 11 | 2026-06-02 | review  | §1 ownership: strip *all* trailing `*` markers (multi-level deref) | F67 #847 |
| 12 | 2026-06-02 | review  | §2 essentially no comments — maintainers strip even "why" comments | #850 (zhangziyao111) |
| 13 | 2026-06-02 | bug-fix | §1 ownership: `IsTrackedType` also gates assignment move-consumption — don't broaden it blindly | F64 (deferred) |
| 14 | 2026-06-02 | bug-fix | §3 a fix isn't done if its repro trades one false positive for another | F19 (deferred) |
| 15 | 2026-06-02 | bug-fix | §1 Sema: invoke a discard-context check from ALL value-discarding positions (conditions, for-inc), not just `ActOnExprStmt` | F22 #851 |
| 16 | 2026-06-02 | bug-fix | §1 Sema: a guard's wrapper list must cover `ArraySubscriptExpr` too (e.g. `&_Mut "s"[i]`) | F36 #852 |
| 17 | 2026-06-02 | bug-fix | §1 ownership: field-state updates must cover the `*`-suffix (deref) family, not just `.`-prefix | F23 #853 |
| 18 | 2026-06-02 | bug-fix | §1 Sema/analysis: gate evaluation-dependent checks on `!isUnevaluatedContext()` (sizeof/_Alignof operands) | F37 #854 |
| 19 | 2026-06-02 | bug-fix | §1 borrow: DefUse needs `VisitArraySubscriptExpr` — `a[i]=X` USES (not defines) the base | F39 #855 |
| 20 | 2026-06-02 | bug-fix | §1 nullability: FieldPath builders must fold the deref base (`UO_Deref`) into the key, else `(*X).f` paths collide | F33 #856 |
| 21 | 2026-06-02 | bug-fix | §1 ownership: sibling check arms must agree on Null-state (checkCastField vs checkCastOPS) | F43 #857 |
| 22 | 2026-06-02 | bug-fix | §1 nullability: absent qualifier ≠ "unknown" — an unannotated pointer pointee is Nullable by BSC default, so compatibility guards must default it before the `LHS && RHS` check, not short-circuit | F66 #858 |
| 23 | 2026-06-02 | bug-fix | §1 ownership: a qualifier check on a MemberExpr base must look *through* deref/member wrappers (`(*s).f` ≡ `s->f`); testing only the immediate base type lets paren-deref/chained forms bypass it. Fix the predicate (walk wrappers), don't special-case the arrow form | F21 #859 |
| 24 | 2026-06-02 | bug-fix | §1 redecl: per-type BSC qualifier-diff predicates (borrow/owned, nullability) must recurse into `FunctionProtoType` params+return, not just `PointerType` — else a qualifier buried in a fnptr parameter is silently merged. Mirror the pointer-recursion arm with a fnptr arm; count mismatches defer to the C-compat check | F53/F56 #860 |
| 25 | 2026-06-02 | bug-fix | §1 redecl: the *heterogeneous* (safe/unsafe) owned/borrow gate `AreOwnedBorrowQualifiersCompatible` (TypeBSC.cpp) has the same fnptr-recursion hole as the homogeneous gate — flags built from `isPointerType()&&is*Qualified()` miss qualifiers buried in a fnptr param. Recurse through the fnptr's FunctionProto when both sides are function pointers | F57 #861 |
| 26 | 2026-06-02 | bug-fix | §1/§3 safe-zone: when two peer conversion predicates gate one rule, they must agree — the enum→int path checked only size while the raw-builtin path checked signedness. Fix the lagging predicate; gate strictness to `EnumDecl::isFixed()` (unfixed C enums keep impl-defined behavior) and reuse `DoesExprValueRangeFitInType` for value-preserving widenings to avoid over-reject. Probe must separate fixed vs unfixed enum AND value-fitting widenings | F51 #862 |
| 27 | 2026-06-02 | bug-fix | §1 ownership: a temp-leak gate keyed on one AST node kind (`CallExpr`) misses sibling temporary-producers (`CompoundLiteralExpr`). Widen the gate, but scope the wrapper-stripping precisely — strip impcasts to find the *new* kind only, leave the existing kind's matching byte-identical so an adjacent in-flight fix (F14, wrapped CallExpr) is untouched. Cross-diff vs base caught 2 unintended flips (F14) before push | F20/F32/F47 #863 |
| 28 | 2026-06-02 | bug-fix | §3 safe-zone: a hardcoded conversion matrix can have a single inconsistent cell — verify with an N-way differential against sibling cells (`ulong→long` and `ulonglong→longlong` were N, `ulong→longlong` was Y). One-cell flip; keep the documentation table in sync; probe value-preserving widenings (`uint→longlong`) to confirm they use a different cell and aren't over-rejected | F71 #864 |
| - | 2026-06-02 | review | §0 spec-gate: two "clean-looking" fixes (F27 explicit mut→const borrow cast, F68 safe-zone raw subscript) contradicted *deliberate* existing corpus tests; the manual was silent. Per principle #6, a fix that flips an intentional-looking test is spec-to-be-determined — note in TRIAGE.md and defer, don't override | F27/F68 deferred |
| 29 | 2026-06-02 | bug-fix | §1 nullability analyzer: don't negate a fact-set by swapping it post-hoc — `!(A && B)` is a disjunction, so swapping `{null:{p}}`→`{present:{p}}` is unsound. Push negation into the builder via De Morgan (`init(Cond, Negate)`: flip `&&`↔`||`, flip basic-case polarity, move constant-eval in so triviality flips). The precise transform beats the conservative "drop narrowing on compound" (which over-rejects `!(A‖B)`→narrow-both). Verify with `-nullability-check=all` probes: `!(p==null&&c)` rejects, `!(p==null‖q==null)` narrows both | F70 #865 |
| 30 | 2026-06-02 | bug-fix | §1 init analysis: a terminator-operand use-checker must cover every operand POSITION and every terminator KIND — `InitAnalysis::run` checked Call args + Return slot but not the Call *callee* (uninit fnptr call) nor the `SwitchInt` *discriminant* (uninit switch). Add the missing `checkOperand` calls; it already no-ops on `Constant` operands so direct calls need no guard. `_Safe` fnptr-call probes need a `_Safe`-qualified fnptr type or the safe-zone "_Unsafe call forbidden" gate fires first | F88/F90 #866 |
| 31 | 2026-06-02 | bug-fix | §1 init analysis: a write destination that loads a pointer must use-check that load on the WRITE side, not just the read side — the dest-projection loop checked `Deref` but not `Index`/`ConstantIndex`, so `s.p[i]=v` through an uninit pointer field slipped (SIGSEGV). Gate the Index check on the indexed operand being a pointer (real arrays load no pointer); indexed type = `I==0 ? B.getLocal(Base).Ty : Projections[I-1].ResultTy` | F83 #867 |
| - | 2026-06-02 | feedback | §0 process: ALWAYS run `new_fix.sh <name>` BEFORE editing code — it creates the branch AND the fixes/ dir. Jumped straight to editing on `bishengc/15.0.4` for F83; recovered with `git checkout -b` (carries uncommitted edits to the new branch) + manual fix-dir, but verify_fix.sh failed first because no branch/dir existed. **Why:** the workflow keys off the branch name; editing on base risks committing to base. **How to apply:** new_fix.sh first, confirm branch, then edit | F83 process |
| - | 2026-06-02 | review | §0 verify the bug_log's "Fix surface" before trusting it — it's a hypothesis, not a verified patch. F84's documented one-liner (add `InvalidateFieldStatusForVar`) was proven wrong by a diagnostic (clearing ALL narrowing maps at the reassignment still didn't reject the stale deref → narrowing is re-applied from block-entry state, a deeper CFG issue). **How to apply:** for analyzer fixes, first add a throwaway over-broad change (clear everything) to confirm the fix SITE is even on the path; if that fails, the mechanism differs from the writeup — investigate before committing to the approach | F84 dead-end |

| 32 | 2026-06-16 | review  | §1 workflow: `#fix` rebase request means fetch UPSTREAM (`bisheng_c_language_dep/llvm-project`), not `origin`; rebase onto `upstream/bishengc/15.0.4`; force-push; comment outcome | #847,#848,#862 (dengxy2020) |
| 33 | 2026-06-16 | review  | §1 docs: a fix that changes what the spec *permits* (not just renames a diagnostic) must update the relevant User Manual chapter, not only `bsc-errors.md` | #862 (dengxy2020) |
| 34 | 2026-06-06 | review  | §1 safe-zone matrix: never flip a matrix cell globally to fix a target-specific issue — gate on actual type widths; keep matrix doc-comment strictly in sync with runtime guards | #864 (dengxy2020) |
| 35 | 2026-06-08 | review  | §1 diagnostics: target-/width-dependent checks need target-conditional diagnostic text; a universal-sounding message is wrong when rejection is conditional on platform | #864 (dengxy2020) |
| 36 | 2026-06-10 | review  | §3 BSCIR lowering changes require a dump-IR test under `clang/test/BSC/BSCIR/` with FileCheck on the emitted IR structure | #873 (ziruichen12138) |

<!-- Add a numbered row per learning; keep `type` in column 4 for learnings.sh. -->
