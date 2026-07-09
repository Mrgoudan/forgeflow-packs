# MangleBSC.cpp — BSC generic name mangling

Source: `clang/lib/AST/BSC/MangleBSC.cpp` (174 lines). UNMAPPED before 2026-06-18.
Callers: `Mangle.cpp:211-213` (mangleBSCName for any NamedDecl), `TypePrinter.cpp:1524`,
`StmtPrinter.cpp:1180+` (call-site name in rewrite). Drives the SYMBOL NAME of every
monomorphized generic instantiation. A collision = two distinct instantiations share one
symbol → codegen emits one body, the other silently aliases/overrides → WRONG ANSWER.

## getBSCTypeName (MangleBSC.cpp:88-146) — UNPROBED
**Invariant**: distinct QualTypes must map to distinct mangled strings. Implementation is
`QT.print(OS, Policy)` (Policy.MangleWithSafeQualifier=true) then a char-substitution table
(`*`→P, `(`→LP, `)`→RP, `[`→LB, `]`→RB, `,`→COMMA, space→`_`). So mangling = the PRINTED
type spelling. Any two types that PRINT identically under this policy COLLIDE.
**Peers**: TypePrinter.cpp:179-226 AppendTypeQualList (prints _Owned/_Borrow/_ArrayElem under
MangleWithSafeQualifier; NO nullability here — Const/Volatile/Restrict + the 3 BSC safe quals);
TypePrinter.cpp:1791-1830 printAttributedBefore (prints _Nonnull/_Nullable/_Null_unspecified
ONLY while the type is a sugared AttributedType); G01 (ConditionalType canonicalization strips
_Nonnull sugar — TypeBSC.cpp:529).
**Candidates**:
1. (G01-family / composition) Two instantiations differing ONLY in NULLABILITY of a type arg
   (`f<int*_Nonnull>` vs `f<int*_Nullable>`): if the stored TemplateArgument is canonicalized
   (sugar lost, like G01) OR the print policy drops nullability, both print `int_P` → SAME
   symbol → one body wins → runtime wrong answer / soundness break. TOP — directly composes
   with the confirmed G01 nullability-sugar-loss root, distinct CODE SITE (mangler/printer).
2. (integral truncation) getBSCArgName (:158-172) negative integral → `"n"+to_string(-getExtValue())`,
   positive → `to_string(getExtValue())`. getExtValue() is int64. Two const-generic args differing
   above 64 bits, or a value vs its 2^64-shifted twin, collide. Also `n5` (arg=-5) could in principle
   collide with a type-arg name; separator is `_` so boundary is `_n5` vs `_<type>` — low risk.
3. (separator ambiguity) getBSCTemplateArgsName joins args with `_`; getBSCTypeName turns spaces
   into `_`. A multi-word type-arg name (`unsigned_long`) adjacent to the `_`-separator could be
   reparsed at a different arg boundary, but the strings still differ char-for-char → no collision
   unless two distinct (type-list) tuples produce the identical char sequence. Low risk.

## 2026-06-18 PROBE OUTCOME
- Mangling collision (candidates 1-3) NOT FOUND: const-generic (idv_5/idv_n5), typedef-collapse
  (BigT→longlong, correct), fnptr/pointer (LPPRP/COMMA/P/PP), all mangle distinctly. getBSCTypeName
  + getBSCArgName robust for the probed shapes.
- BUT the nullability-collision probe surfaced an UPSTREAM root: a `_Nonnull`/`_Nullable` qualifier on
  a generic function's TYPE-TEMPLATE-ARGUMENT is canonicalized away (instantiation prints `f<int *>`,
  not `f<int *_Nonnull>`) — same canonicalization family as G01 (ConditionalType desugar) but a DIFFERENT
  entry point (generic-function template-arg capture/substitution). **CONFIRMED-new soundness FN** (see the
  dedicated note below + bug_log when filed). Mangling itself is not the bug; the sugar is gone before the
  mangler ever sees it.

## CONFIRMED-NEW (2026-06-18): generic param/return _Nonnull sugar dropped → arg-passing gate FN
**Root site**: type-template-argument canonicalization for BSC generic FUNCTIONS — an explicit
`int *_Nonnull` (an AttributedType sugar) supplied as `f<int *_Nonnull>` is stored/substituted as the
CANONICAL `int *` (nullability AttributedType stripped). Entry: standard clang TemplateArgument
canonicalization reached via SemaTemplateInstantiateDeclBSC.cpp:79+/190+ (SubstTemplateArguments) →
the substituted param/return type loses `_Nonnull`. Same canonicalization-strips-nullability-sugar
FAMILY as G01 (TypeBSC.cpp:529 ConditionalType::desugar), DIFFERENT site (generic-fn instantiation).
**Invariant violated**: the nullability contract on a generic param/return must survive instantiation,
so `f<int *_Nonnull>(nullable_arg)` is rejected exactly as the non-generic `f_nn(nullable_arg)` is.
**Symptoms**:
- FN (soundness): `sink<int *_Nonnull>(q)` with `_Nullable q` ACCEPTED (rc=0) — non-generic baseline rejects.
- FP (mirror): `int *_Nonnull a = idt<int *_Nonnull>(p)` with `_Nonnull p` REJECTED ("nonnull cannot be
  assigned by nullable") — non-generic baseline clean. (Deduced `idt(p)` same FP.)
**Containment**: when the body itself derefs `x` (`x->v`), the path-sensitive checker re-catches the
deref because the de-sugared instantiation tracks the arg as nullable into `x` — so a direct in-`_Safe`
runtime null-deref needs the body to PROPAGATE rather than deref. The violated invariant is the static
arg-passing / return-assign contract gate (same containment as G01). Severity MEDIUM.
**Repro**: /tmp/explorer_probe.jDw8lF.cbs (FN, clean=bug); baseline /tmp/explorer_baseline.faozRv.cbs (rejects).

## getBSCTypeName/getBSCTemplateArgsName MangleWithSafeQualifier — CONFIRMED-G14 (2026-06-22)
- Struct record-name path drops _Owned/_Borrow/_ArrayElem (MangleWithSafeQualifier=false default); S<int*> & S<int*_Owned> collide to S_int_P → -rewrite-bsc duplicate-def → invalid C. Function path OK (MangleBSC.cpp:76 sets flag). FILED G14 (MEDIUM).

## 2026-06-25 PROBE OUTCOME — candidate #1 CONFIRMED at the MANGLE layer (distinct from G12)
- The 2026-06-18 probe above correctly found the Sema-time nullability-strip FN (filed G12) but STOPPED
  SHORT of the mangle-layer symptom. Re-probed: even when both call sites are type-CORRECT (no G12 FN
  exercised), the three specializations `pass<int *>`, `pass<int *_Nonnull>`, `pass<int *_Nullable>`
  mangle to the SAME symbol `pass_int_P`.
- EVIDENCE: `nm` on normal `-O0` codegen shows EXACTLY ONE `pass_int_P` symbol for the 3 distinct
  specializations (/tmp/g17_fn.cbs; runs exit 0 — merged body is pointer-identity-correct). `-rewrite-bsc`
  emits ONE `pass_int_P` def + ONE `Box_int_P` struct def for 3 nullability instantiations each
  (repro/G17_*.cbs; /tmp/ftest9/ftest13.cbs). NO redefinition error (silent merge) — unlike G14 which errors.
- ROOT (different from G12's Sema-substitution strip): `MangleBSCContext::mangleBSCName` (MangleBSC.cpp:23-25)
  calls `adjustForRewritingBSC()` (PrettyPrinter.h:99-103) which sets `PrintCanonicalTypes=true`;
  `getBSCTypeName` (:88-92) calls `QT.print` → `splitAccordingToPolicy` (TypePrinter.cpp:268-269)
  canonicalizes the arg type → AttributedType nullability sugar stripped BEFORE `printAttributedBefore`
  (:1791-1832, the only nullability emitter) is reached → mangled name omits nullability → collision.
- ASYMMETRY (generic-mangle-specific): NON-generic `pass_nn(int *_Nonnull)` rewrites PRESERVING `_Nonnull`
  (/tmp/ftest14.cbs) — only the generic monomorph mangle path sets PrintCanonicalTypes. `_Owned`/`_Borrow`/
  `_ArrayElem` SURVIVE (real Qualifiers bits, kept by getCanonicalType) → `pass<int *_Borrow>` = `pass_int_P_Borrow`
  (distinct). So the collision is NULLABILITY-SPECIFIC.
- DISTINCT: G14's fix (set MangleWithSafeQualifier=true on struct path) does NOT fix this — nullability is
  AttributedType, never emitted by AppendTypeQualList regardless of flag. G12's fix (preserve nullability
  through Sema substitution) would NOT fix the mangle collision either (the mangler re-canonicalizes via
  PrintCanonicalTypes even if Sema preserved the sugar). Different file/line (TypePrinter.cpp:268-269),
  different symptom (silent single-def MERGE vs G14's redefinition ERROR vs G12's call-site check FN).
- SEVERITY MEDIUM (fails closed): merged body type-correct for pointer-identity; semantic loss = nullability
  contracts erased from codegen/rewritten-C symbol+type space. NOT filed (report-only per 2026-06-25 task).
  Repro: repro/G17_generic_nullability_mangle_collision.cbs.

## getBSCTypeName mangle-collision candidate — RESOLVED 2026-06-25 (source read; GLM lead closed)
**Mechanism** (MangleBSC.cpp:88-146): `QT.print(OS, Policy)` then char-substitution (*→P, (→LP, etc.).
The Policy has `MangleWithSafeQualifier=true` (set :76 for fn mangling) so _Safe IS printed/mangled — BUT
there is NO equivalent flag for _Owned/_Borrow, so the print policy DROPS them.
**Collision accounting** (the UNPROBED "distinct types print alike" candidate is now RESOLVED):
- _Owned vs _Borrow: PRINT IDENTICALLY (policy drops both) → same mangled name = **G14** (FILED, the
  owned/borrow generic-struct mangle collision). The codegen mangler shares this getBSCTypeName root → FOLD-G14.
- _Nonnull vs _Nullable: STRIPPED in monomorphization before mangling = **G12** (FILED).
- _Safe vs _Unsafe: PRESERVED (MangleWithSafeQualifier) → distinct, NO collision (probe-confirmed 2026-06-25).
- const: PRESERVED → distinct (probe-confirmed).
→ NO NEW mangle collision beyond G14/G12; the surface is fully accounted for. GLM's mangling deep-dive would
  have re-derived G14. Lead CLOSED by source reading (GLM down anyway).

## char-substitution collision (punctuation-named struct) — SHAPE-REJECTED 2026-06-25 (nm-confirmed)
- Probe: `int use<T>(T x){}; use<int *>(p)` vs `use<struct int_P>(s)` — `int *` mangles via *→P,space→_ to
  "int_P". nm shows DISTINCT symbols: `use_int_P` vs `use_struct_int_P`. The printed struct type carries the
  "struct" keyword ("struct int_P" → "struct_int_P"), so it does NOT collide with `int *` ("int_P"). The char-
  substitution (*→P,(→LP,COMMA,...) is NOT exploitable for a type-vs-punctuation collision (struct prefix +
  multi-char substitutions for LP/RP/COMMA differentiate). /tmp/claude-998/mcoll.cbs.
- CONCLUSION: G14 (owned/borrow, where the print policy genuinely DROPS the qualifier) is the ONLY mangle
  collision; the char-sub mechanism itself is sound. Mangling-collision surface fully closed.

## getBSCArgName integral negation overflow — CONFIRMED-G18 (FILED 2026-06-25)
- The earlier note "getBSCArgName robust for probed shapes" was incomplete: it tested only SMALL values
  (idv_5/idv_n5). At the 64-bit boundary it's NOT robust: negative-integral mangle `"n"+to_string(-getExtValue())`
  does `-getExtValue()` which is signed-overflow UB for INT64_MIN → wraps → "n-9223372036854775808" (embedded '-').
  -rewrite-bsc emits this as a C identifier → invalid C (gcc rejects). FILED G18 (MEDIUM, G14-class). The int
  case is fine (INT_MIN → 2147483648 fits int64). Fix: APSInt .abs()/unsigned magnitude, not `-getExtValue()`.
