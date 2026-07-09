# ParseDeclBSC.cpp — BSC declaration/generic parser

Source: `clang/lib/Parse/BSC/ParseDeclBSC.cpp` (1925 lines). The BSC generic
**declaration** parser. Created 2026-06-18 to backfill the parser-reading gap:
the generic crashes G02–G05 were FUZZ-found (crash stack → root) and never had a
reading note; this file is read **block-granularity** (BACKLOG #8 / DESIGN.md) —
skeleton the dispatch arms, set-diff vs the input universe, read only the arms that matter.

## Generic-decl entry points (skeleton)
- `ParseBSCGenericDeclaration` (:1658) — entry for `template`-angle decls.
- `ParseBSCDeclarationAfterTemplateAngles` (:1740) — replays prefix+angle+suffix toks,
  then delegates to core `ParseSingleDeclarationAfterTemplate` (dispatch on what follows
  `<...>`: struct/union/typedef/function). The G03/G04 crashes surface downstream here
  (ActOnTagFinishDefinition / ParseClassSpecifier on a rejected spec body).
- `ParseBSCTemplateParameters` (:1769) — parse `< params >`, incl. angle-close handling.
- `ParseBSCTemplateParameterList` (:1805) — loop over params (comma-separated).
- `ParseBSCTypeParameter` (:1835) — one param (type `T` / const-generic `int N`).

## ParseBSCTemplateParameters angle-close (:1779-1801) — CONFIRMED-G11 (block-granularity find)

**Invariant**: after the param list, consume the closing `>`; if the lexer merged it with
following `>`s into `>>`/`>>>`, split off one `>` and re-lex the rest (mirrors C++).
**Arm set (present)**: `tok::greater`, `tok::greatergreater`, `tok::greatergreatergreater`.
**Input universe (set-diff)**: the closing `>` can also abut `=` → `tok::greaterequal`
(`typedef A<T>=...`, no space). **MISSING ARM: `greaterequal`** → the close isn't consumed,
param list malformed, alias undefined → misleading "unknown type name". **CONFIRMED-G11**
(MEDIUM FP, FILED IJP3AP). Spaced `A<T> = ...` works. Fix: add a greaterequal split arm
(consume `>`, re-lex `=`), same shape as the greatergreater split at :1789.
**Peers**: core `Parser::ParseTemplateParameters` (the C++ analog — does it split `>=`? likely
also only `>>`); the lexer's angle-bracket tokenization.

## Crash sites in this parser (fuzz-found, G02/G03/G04/G05 — see bug_log)
- G02 — `ParseTemplateArgumentList` ConsumeToken assert on a parenthesized const-generic arg
  (core clang, reached via the BSC generic-arg path). HIGH, filed.
- G03 — generic-struct specialization body → `ActOnTagFinishDefinition` SIGSEGV (shipping). filed.
- G04 — generic-alias-as-struct-spec → `ParseClassSpecifier` isa<>-null (shipping). filed.
- G05 — template-id `A<int>::foo` member-def → `getCurrentClass` assert (assert-only). filed.

## Candidates (unprobed, from the skeleton)
1. `ParseBSCTypeParameter` (:1835) — default-param `T=int` handling: `=` inside the param list
   (vs the `>=` at the close). m10 (`struct S<T=int>`) was clean — likely handled/rejected. UNPROBED-low.
2. `ParseBSCTemplateParameterList` error-recovery loop (:1812-1818) — skip-to-comma/greater on a
   bad param; could it loop or mis-recover on an unusual token? Probed indirectly (m-batch clean). low.
3. Does the `greaterequal` gap also hit a generic **function** decl followed by `=`? Functions have no
   `<...>=`; only type-aliases do. So G11 is alias-specific. (Bounded.)

## generic-syntax edge fuzz (assert build) — probe 2026-06-25 (GLM-generics push) — no crash
- 5 forms on the assert build (~/bsd/llvm-project, own headers): `P<int,P<int,int>>` (>> token), `Box<void(*)(int)>`
  (fnptr arg), `Box<Box<Box<int>>>` (deep nest) all rc=0 accepted; `Box<int,>` (trailing comma), `Box<>` (empty
  args) cleanly rc=1 rejected. NO assert/SIGSEGV on any. The generic parser handles these edge forms gracefully
  (no new G02-G05-style crash). The >> double-angle-close (classic C++ parser pitfall) works. SOUND.
