# SemaTemplateInstantiateDeclBSC.cpp — reading notes

Source: `clang/lib/Sema/BSC/SemaTemplateInstantiateDeclBSC.cpp` (300 lines).
Instantiates BSC generic decls (methods/traits) — the FREE-FUNCTION and generic-STRUCT
instantiation paths live upstream in core clang `TemplateDeclInstantiator` /
`SubstFunctionType` / `SubstType` (TreeTransform), NOT in this BSC file. The file's
:79-225 range is `VisitBSCMethodDecl` (member functions — OUT OF SCOPE). G12's attribution
to "SemaTemplateInstantiateDeclBSC.cpp:79-225" is loose; the real sugar-strip happens in the
core template-arg-substitution path shared with `SemaTemplateInstantiate` (non-BSC).

## Free-function / generic-struct type-template-arg substitution (core path) — PROBED-SOUND

**Invariant**: substituting a type-template-argument `T = int *_Owned` into a generic free
function or generic struct preserves the `_Owned` AttributedType-sugar on the monomorphized
decl, so the body ownership analysis (leak / move / return / field-leak) treats the param/
field/return as owned and tracks it correctly.

**Peers**:
- `ConditionalType::desugar()` (TypeBSC.cpp:529) — the G01 path; canonicalizes and strips
  nullability sugar. Different surface, same "canonicalization drops AttributedType" theme.
- `getDefNullability` (BSCNullabilityCheck.cpp:658) — the G12 gate; reads the AttributedType
  layer for nullability. The ownership analog reads `_Owned`/`_Borrow` which survive canonical.
- `IsTrackedType` / ownership TransferFunctions (BSCOwnership.cpp) — consume the substituted
  type; see `_Owned` because `getCanonicalType` keeps real qualifiers, strips only attribute-sugar.
- MangleBSC: `getBSCFunctionMangleName` sets `MangleWithSafeQualifier=true` (correct);
  `getBSCTemplateArgsName` struct-record path does NOT (→ G14, struct-only mangle collision).

**Candidates** (all PROBED-SOUND on fresh bin 34e6f26e, 2026-06-23):
1. `sink<T>(T x){}` + `sink<int*_Owned>(p)`, body drops x → **leak still fires**
   ("memory leak of value: x"); `_Owned` present in mangled specialization name
   `sink<int *_Owned>`. — refutes the G12-ownership-analogue hypothesis.
2. `id<T>(T x){return x;}` + `id<int*_Owned>(p)` then `safe_free(p)` → move diagnosed
   (via cast gate `incompatible _Owned types`); return-move tracked through substitution.
3. `struct Box<T>{T v;}` + `Box<int*_Owned>` dropped w/o freeing v → **field-leak fires**
   ("field memory leak of value: b, b.v is leak"); vg rejects at compile (no silent codegen leak).
4. `Box<int*_Owned _Nullable>` null-field interaction → SHAPE-REJECTED (null-owned cast/global
   idioms forbidden identically in generic & non-generic; both reject same way — no asymmetry).
5. `make<T>(void){return ...}` + `make<int*_Owned>()`, caller drops p → **caller leak fires**
   ("memory leak of value: p"); identical to non-generic baseline. Return-side owned tracked.

**Conclusion**: `_Owned`/`_Borrow` AttributedType sugar is NOT stripped during generic
monomorphization, in contrast to `_Nonnull`/`_Nullable` (G12, a real FN). The reason
(per _probed.md:5648-5649 + G01 blast-radius): `getCanonicalType` keeps the real BSC
qualifiers (`_Owned`/`_Borrow` are represented as qualifiers on the canonical type, not as
nullability-style AttributedType-sugar layers), so the substitution canonicalizes away only
the nullability attribute-sugar. The ownership analyzer reads qualifiers → sees owned.
No ownership-analogue of G12 exists on this surface. The 2026-06-17 gm1/go1/gorun SOUND
results re-confirmed on the current binary.

**Distinct-from checks**: G10 (owned-array bypass via `typedef Arr<T>=T[N]` — a
declarator-vs-substitution check-skip, NOT sugar-strip; different surface, owned-array only);
G14 (struct mangle collision — TypePrinter record-name path drops qual from the *printed
name*, Sema still tracks the type distinctly; not a tracking FN); G12 (nullability sugar
strip — confirmed distinct, the nullability dimension; ownership dimension is SOUND here).

## generic struct with _Owned-typed field `Box<int *_Owned>` — probe 2026-06-25 (GLM-generics push)
- Probe: `struct Box<T>{ T val; }; struct Box<int *_Owned> b; b.val=safe_malloc...;` (unfreed) → FLAGGED
  `field memory leak of value: b, b.val is leak`. Monomorphization preserves the owned-ness of the T-typed
  field → leak tracked. SOUND (no monomorph-loses-owned FN). Distinct from G10 (generic TYPE-ALIAS owned leak).

## generic struct copy with owned field `Box<int *_Owned> c = b` — probe 2026-06-25 (GLM-generics push)
- Probe: copy a generic struct whose T-field is owned, free only c.val → runtime 1 alloc/1 free, ERROR SUMMARY 0.
  The struct copy MOVES the owned field (b moved-from, b.val nulled) → no double-free, no leak. SOUND (move-on-
  copy correct for owned-containing generic-struct instantiations). Box<int*_Owned> field-leak also tracked (above).

## generic struct borrow field `Box<int *_Borrow>` loan tracking — probe 2026-06-25 (GLM-generics push)
- Probe: `struct Box<T>{ T val; }; b.val=&_Mut x; int *_Borrow q=&_Mut x;` → REJECTED `cannot borrow x as
  mutable more than once` + note at the b.val borrow. The loan from the T-borrow field (via monomorphization)
  is tracked → conflict caught. SOUND. Generics×ownership + generics×borrow both sound: field-leak tracked,
  copy=move, loan-tracked, "even indirectly" guard re-checked at monomorph. Monomorphization preserves
  owned/borrow semantics correctly.

## nested generic owned `Box<Box<int *_Owned>>` — probe 2026-06-25 (GLM-generics push) — SOUND
- Probe: doubly-nested generic, bb.val.val owned unfreed → FLAGGED `field memory leak of value: bb, bb.val.val
  is leak` (full nested path). Monomorphization preserves owned-ness through 2 nesting levels; leak tracked.
  Generics×ownership comprehensively SOUND (field/copy/borrow/guard-recheck/const-gen/nested all sound).

## adjustFunctionTypeForInstantiation (SemaTemplateInstantiateDeclBSC.cpp:49-62) — read 2026-06-25
INVARIANT: the instantiated generic FUNCTION's type carries the TEMPLATE's ExtInfo (calling-conv, noreturn,
BSC safe-ness) — if Orig vs New ExtInfo differ, it rebuilds the type with Orig's ExtInfo onto New's EPI.
PEERS: InitFunctionInstantiation, VisitBSCMethodDecl (member methods — OUT OF SCOPE), adjustForRewritingBSC.
NOTE: rest of this file = traits (VisitTrait*Decl) + member methods (BSCMethodDecl) — OUT OF SCOPE.
CANDIDATES (all RESOLVED):
1. safe/owned/borrow dropped at fn instantiation — SHAPE-REJECTED: probes confirm _Safe/owned/borrow/const all
   preserved through generic-fn monomorph (2026-06-25 logs).
2. ExtInfo force-override wrong when instantiation legitimately changes CC — SHAPE-REJECTED: generics don't
   change CC; forcing the template's ExtInfo is correct.
3. castAs<FunctionProtoType> (:52/:54) crash on K&R unprototyped generic fn `f<T>()` — SHAPE-REJECTED
   (assert+canonical both rc=0): BSC treats `()` as prototyped (C++-template `(void)` semantics), no
   FunctionNoProtoType reaches the cast. /tmp/claude-998/cgkr.cbs.
adjustFunctionTypeForInstantiation SOUND.
