# Open-bug fix surfaces (precise roots) — consolidated 2026-06-25

Actionable fix pointers for the findings OPEN on the current binary (34e6f26e, = origin HEAD).
Derived from this session's end-to-end source reads. Severity/repro in bug_log.md; status in findings.tsv.

## Ownership / init (field-level)
- **F75** (HIGH, double-free) — `OwnershipImpl::merge` (BSCOwnership.cpp:231) UNIONs owned-state across CFG
  predecessors: `OPSStatus[VD] |= BV` (BitVector OR, :245/:271) + owned-field-set `.insert` union (:257-259/
  :284-285). A field owned in ONE predecessor (moved in another) is restored to owned at the join → moved-state
  lost → double-free. FIX: AND/intersection (owned-at-join = owned-in-ALL-preds), handling the empty-seed (:233).
  Contrast: InitAnalysis::merge MEETs correctly.
- **F99** (init FP) — `getFieldPath`/`getFieldPathPrefix` (BSCIRInitAnalysis.cpp:864) return None for any Place
  with a Deref projection → `markFieldInit(Place)` (:923) no-ops → `*b.p=v` (b.p set by assignment) never marks
  the pointee init. FIX: handle the Deref-projection place in the field-init path.
- **F97** (init FP, retracted-but-intended) — arrays all-or-nothing: array-field write `break` (:172), array-elem
  not reached (getFieldPath None on Index). FIX (if desired): track constant-index element coverage; or document.
- **F106/F107** (init) — tryPromoteParent (:929): empty-struct sibling never markFieldInit'd (F106 :970-977);
  union narrow-variant write marks whole union Init w/o byte-coverage check (F107 :948-964 + transferStatement
  :180-188 + markAllFieldsInit union all-variants + checkOperand over-read).

## Heterogeneous _Safe/_Unsafe redecl (user's active area; qualifier dim FIXED via 8e39447/3693a28)
- **F103** (HIGH nullability) — PRECISE ROOT: `AreParamTypesCompatible` (TypeBSC.cpp:256, lambda in
  areFunctionTypesCompatibleForHeterogeneousRedecl) does `AttributedType::stripOuterNullability` (:261) on both
  sides then checks ONLY `AreOwnedBorrowQualifiersCompatible` (:274/:306) — nullability is stripped and NEVER
  re-compared → `_Nonnull` vs `_Nullable` redecl accepted. FIX: add a nullability-compat check (safe side must be
  equal-or-stronger) beside the owned/borrow check. (Sibling: AreOwnedBorrowQualifiersCompatible :154 handles
  owned/borrow incl. fnptr recursion = the 8e39447/F57/F77-fixed dimension.)
- **F105** (HIGH owned-ADD) — PRECISE ROOT: `AreOwnedBorrowQualifiersCompatible` (TypeBSC.cpp:154) checks only the
  DROP direction (`UnsafeIsOwned && !SafeIsOwned → false`, :168-173) — there is NO ADD-direction check. A safe decl
  ADDING `_Owned`/`_Borrow`/`_ArrayElem` the unsafe def lacks passes → safe caller takes/frees ownership the impl
  never had → invalid-free. FIX: add the reverse guards (`SafeIsOwned && !UnsafeIsOwned → false`, + borrow/arrayelem).
- **F104** (HIGH ensure_init) — the hetero-redecl gate (AreParamTypesCompatible) does NOT check the ensure_init
  ExtParameterInfo, while the ASSIGNMENT/cast path does (`CheckEnsureInitFunctionPointerType` SemaBSCOwnership.cpp:963
  `LHSExt.isEnsureInit() && !RHSExt.isEnsureInit() → error`). Ordering-dependent (which decl's ExtParameterInfo
  survives the merge). FIX: check ensure_init compat in the redecl gate too.

### UNIFYING ROOT (F103/F104/F105/F117) — the redecl gate lacks parity with assignment/cast checks
The heterogeneous _Safe/_Unsafe redecl gate (areFunctionTypesCompatibleForHeterogeneousRedecl / AreParamTypes
Compatible, TypeBSC.cpp:211/256) is systematically WEAKER than the assignment/cast compat checks — it MISSES:
nullability (F103; assignment checks, redecl strips at :261), ensure_init (F104; CheckEnsureInitFunctionPointerType
:963 checks on assign, redecl doesn't), owned-ADD (F105; only DROP guarded :168-173), and safety-zone-of-the-body
(F117; the fnptr CAST rejects unsafe→safe, redecl doesn't reconcile). ONE coherent fix: bring the redecl gate to
parity with the assignment/cast paths (add the 4 missing checks). The owned/borrow-DROP + nested-fnptr-recursion
dimension was already brought to parity (8e39447/F57/F77 fixed).
- **F76** (MEDIUM) — nested-fnptr-param variance at the ASSIGNMENT path (DoPointerTypesSatisfyAssignmentConstraints
  Impl) does NOT recurse into nested fnptr params; the REDECL path was fixed by 8e39447. FIX: mirror 8e39447's
  recursion into the assignment variance check.
- **F117** (HIGH, do-not-file/user-area) — `_Safe`-decl + `_Unsafe`-def: the redecl merge
  (areFunctionTypesCompatibleForHeterogeneousRedecl / MergeFunctionDecl) does NOT reconcile the function's own
  safety zone → a `_Safe` caller invokes the unchecked `_Unsafe` body → SIGSEGV. The fnptr-CAST path DOES check
  this ("unsafe fnptr → safe fnptr forbidden"); FIX: mirror that safety-zone check into the redecl gate (reject
  the mismatch / treat merged fn as the stricter zone).

## Nullability / rewriter / mangler / generics (filed)
- **F14** (HIGH leak) — CheckTemporaryVarMemoryLeak (SemaBSCOwnership.cpp:534) bare `dyn_cast<CallExpr>(E)`, no
  IgnoreParens/recursion → any wrapper ((e), (c,e), c?e:e) bypasses. FIX: IgnoreParenCastsSafe + recurse comma/
  conditional (as eadb9c5 did for the sibling :547).
- **F31** (HIGH null-call) — VisitCallExpr (BSCNullabilityCheck.cpp:649) never inspects CE->getCallee() nullability
  → nullable fnptr `fp()` called unchecked → SIGSEGV. FIX: check getExprPathNullability(CE->getCallee()).
- **F116** (MEDIUM) — -rewrite-bsc keeps `_Nullable`/`_Nonnull` in struct/union FIELD declarations (only that path;
  params/returns/locals/typedefs strip) → output not gcc-portable. FIX: strip nullability in the field type-spelling path.
- **G12** (nullability), **G14** (owned/borrow mangle collision), **G17** (union-field guard not re-checked at
  monomorph), **G18** (getBSCArgName negation overflow at INT64_MIN / unsigned>INT64_MAX → malformed mangle), **G10**
  (generic-type-alias owned-array bypass) — see bug_log entries (each has a fix pointer).

## Temp-owned-leak cluster (F14/F20/F22/F47) — UNIFYING ROOT
`CheckTemporaryVarMemoryLeak` (SemaBSCOwnership.cpp:534) detects a discarded `_Owned` temporary. TWO coverage gaps:
1. EXPRESSION coverage (the predicate): bare `dyn_cast<CallExpr>(E)`, NO IgnoreParens/recursion, NO CompoundLiteralExpr
   → `(e)`/`(c,e)`/`c?e:e` wrappers bypass (F14); a CompoundLiteralExpr owned temp `(struct S){.p=mk()}` bypasses
   (F20, and via member access F47). FIX: handle CallExpr + CompoundLiteralExpr + recurse paren/comma/conditional
   (IgnoreParenImpCasts), as eadb9c5 did for the sibling CheckMoveVarMemoryLeak (:547).
2. POSITIONAL coverage (call sites — SemaStmt.cpp:55 expr-stmt, SemaExprMember.cpp:1288 member-base, SemaExpr.cpp
   :16548-49 COMPARISON-op operands, :17111 deref): MISSES if/while/for/do/ternary CONDITIONS, for-init/inc, and
   `&&`/`||`/comma operands (the :16548 `isComparisonOp` gate, F22). FIX: widen the operand gate to BO_LAnd/BO_LOr/
   BO_Comma + add a CheckFullExpressionLeak at every discarded-value/full-expression point.
ONE coherent fix: a single CheckFullExpressionLeak (handling all owned-temp-producing exprs through wrappers) called
at EVERY full-expression position closes F14+F20+F22+F47 together.

## Nullability-indirect-call cluster (F28/F31) — UNIFYING ROOT
`TransferFunctions::VisitCallExpr` (BSCNullabilityCheck.cpp:~649) gates its nullability checks on
`CE->getDirectCallee()`. For an INDIRECT call (`fp(args)`, getDirectCallee()==null) it skips BOTH:
- the ARG-vs-param nullability check (F28: nullable arg into nonnull param of an fnptr accepted → null deref);
- the CALLEE nullability check (F31: nullable fnptr `fp()` called unchecked → null-call SIGSEGV; getCallee() never inspected).
FIX (one site): when getDirectCallee() is null, derive the FunctionProtoType from `CE->getCallee()->getType()`
and check arg-vs-param nullability via getDefNullability (F28); AND check `getExprPathNullability(CE->getCallee())`
for a nullable callee (F31). Both gaps live in the same indirect-call branch of VisitCallExpr.

## Guard-bypass-via-generic vein (G10/G17) — BLAST RADIUS BOUNDED (2026-06-26 S3)
Systematic test of every BSC type-formation guard × generic route. Two guard families:
- CheckOwnedOrIndirectOwnedType (SemaBSCOwnership.cpp:110) — 3 call-sites: array (SemaType.cpp:5202),
  union-field (SemaDecl.cpp:18723), global-var (SemaDecl.cpp:8556).
- CheckArrayElemQualifierRules (SemaBSCOwnership.cpp:69, has !isDependentType() skips at 77/84/92) — _ArrayElem pointee.
RESULTS (direct REJECTED in all cases; which generic route bypasses):
- array-of-owned via generic type-ALIAS (typedef Arr<T>=T[3]) -> BYPASS = G10 (filed). SemaType:5202 not re-run for
  alias-substituted array types.
- owned-in-union via generic union field -> BYPASS = G17 (filed).
- array-of-owned via PLAIN generic field (T data[4]) -> RE-CHECKS (sound).
- owned-GLOBAL via alias (OG<int*_Owned> g) -> RE-CHECKS (sound; alias resolves, SemaDecl:8556 fires).
- _ArrayElem-invalid-pointee via plain generic AND via alias -> RE-CHECKS (sound; CheckArrayElemQualifierRules re-runs).
- _Nonnull-on-non-pointer via generic -> RE-CHECKS. leak/uninit/move (body-flow) via generic -> RE-CHECK.
CONCLUSION: the bypass is NARROW — only the array-element check (SemaType:5202, alias route) and union-field
(SemaDecl:18723) skip re-checking. NO third instance exists. G10 fix = re-run array-element owned-check after
alias substitution; G17 fix = re-run union-field owned-check at instantiation. Other guards already re-check.

### G10/G17 blast-radius CORRECTION (2026-06-26, GLM autonomous Explorer find)
The autonomous GLM Explorer (restored harness) found a THIRD bypassed context I missed: the STATIC-LOCAL owned
guard (SemaDecl.cpp:8556, "type of static local variable cannot be qualified by '_Owned'") is NOT re-checked when
the type arrives via a generic PARAM: `void f<T>(){ static T r; }` instantiated T=owned bypasses it.
- My earlier audit tested owned-FILE-GLOBAL via type-ALIAS -> re-checked (sound). But static-LOCAL via generic
  PARAM is a different route through the same SemaDecl:8556 call-site, and it IS bypassed.
- IN-SCOPE (owned pointer): BACKSTOPPED by the nonnull-init guard ("type contains nonnull pointer must be properly
  initialized") since an uninit static owned pointer is caught -> NOT a soundness hole in-scope, NOT fileable.
- Exploitable form needs a zero-initializable _Owned STRUCT (no nonnull field) = OUT OF SCOPE.
- ROOT: same as G10/G17 (CheckOwnedOrIndirectOwnedType contexts not re-run at monomorphization). The maintainer's
  fix must re-run ALL three contexts (array/union/static-local) post-instantiation, not just G10's + G17's.
CORRECTED CONCLUSION: the guard-bypass-via-generic root spans >=3 contexts (array-alias=G10, union-field=G17,
static-local-param=this). Only G10/G17 are independently exploitable in-scope; static-local is backstopped/OOS.

### Redecl-gate PRECISE SHARED ROOT (2026-06-26 S3, source-confirmed TypeBSC.cpp:211-325)
areFunctionTypesCompatibleForHeterogeneousRedecl uses ONE helper `AreParamTypesCompatible` (lambda @:256) for
BOTH the RETURN type (called @:309-310) AND every PARAM (called @:316-317). That helper:
- :262-263 `stripOuterNullability` on both sides  -> nullability NEVER compared = F103 (params) AND the same gap on
  the RETURN type (confirmed: `_Safe int*_Nonnull f(void); _Unsafe int*_Nullable f(void){return (int*)0;}
  _Safe int*_Nonnull g(void){return f();}` -> rc=0; g provably returns null as _Nonnull. Same-zone redecl = rc=1
  "conflicting types"). => the return-nullability variant FOLDS into F103 (identical line); ONE fix at :262-263
  (compare nullability: safe side must be equal-or-stronger) covers return + ALL params.
- :264-269 strips owned/borrow/arrayelem, then :274/:306 `AreOwnedBorrowQualifiersCompatible` (only-DROP) = F105;
  also applies to the RETURN type (shared helper) -> F105's owned-ADD gap spans return + params; one fix covers both.
ALREADY CHECKED by the gate (NOT gaps): param COUNT (:239), variadic (:243), return+param BASE-type compat
(:303 typesAreCompatible), nested safe/unsafe fnptr params (:280-300 recursion = the 8e39447-fixed dim).
F104 (ensure_init ExtParameterInfo) is param-only (return has no ExtParameterInfo). F117 (body safety zone) is NOT
in this gate at all — the gate is DESIGNED to allow the safe/unsafe pair (:227 requires one-safe-one-unsafe); F117's
fix must be at the caller/merge side (treat the merged fn as _Unsafe), not here.
NET maintainer guidance: fix F103+F105 IN-PLACE at AreParamTypesCompatible (:262-263 add nullability cmp; :274/:306
add owned/borrow ADD-direction guard) — this single helper edit closes both dimensions for return AND all params.
