# SemaChecking.cpp — raw-transfer builtin family (`__move_to_raw`/`__take_from_raw` + array variants)

## handleBSCRawTransferBuiltin (SemaChecking.cpp:245-271) — PROBED-clean-negative (2026-05-30)

**Outcome**: all 4 qualifier dimensions {pointee-const, pointee-volatile,
nullability, owned/borrow/arrayelem} round-tripped through the builtin family;
NONE laundered. getUnqualifiedType (L248) only strips TOP-LEVEL quals so the
pointee cv survives inside the PointerType. The nullability copy (L266-269)
merely SEEDS the static result type; the real authority is BSCNullabilityCheck
dataflow which is narrowing-aware and robust to the static seed (a _Nonnull raw
from move_to_raw does NOT suppress null-tracking after `r=NULL`). The
__take_array_from_raw doubled-qualifier pretty-print is a cosmetic AttributedType
sugar artifact (canonical matches; valgrind-clean). NO fileable defect here.

**Invariant**: the result type of a raw-transfer builtin must preserve every
qualifier of the argument EXCEPT the owned/borrow/arrayelem set it deliberately
strips/re-adds, and the nullability it copies must reflect the argument's true
nullability (no Nullable->Nonnull launder, no dropped pointee const/volatile).

**Mechanism**:
- L248 `ResultTy = ArgTy.getUnqualifiedType()` — strips TOP-LEVEL quals only
  (owned/borrow/arrayelem/nullability live at top level; pointee const/volatile
  live inside the PointerType pointee, untouched).
- L249-251 removeLocalOwned/Borrow/ArrayElem (redundant after unqualify but safe).
- L252-264 rebuild Qs; for take[_array]_from_raw add Owned (+ArrayElem for array).
- L266-269 copy nullability via getBSCDefNullability(ArgTy)->ResultTy.
  - raw default = Nullable (BSCNullabilityCheck.cpp:250-251)
  - owned/borrow default = NonNull (BSCNullabilityCheck.cpp:246-249)

**Peers**: getBSCDefNullability/applyNullabilityToType (:155-180),
checkMoveToRawArgumentShape (:198), checkTakeFromRawArgumentShape (:222),
QualType::getUnqualifiedType, removeLocalArrayElem (Type.cpp:1517).

**Candidates**:
1. pointee-const launder via __move_to_raw then free / write — does the raw
   result keep `const T*`? getUnqualifiedType is top-level so const should
   survive; PROBE the round-trip write-through.
2. __take_from_raw of an unannotated raw gives `_Owned _Nullable` (Src raw=Nullable,
   Dst owned=NonNull, mismatch applies Nullable) — that is CONSERVATIVE, sound.
   But __take_from_raw of `_Nonnull` raw → `_Owned _Nonnull`; if a user can make
   the raw _Nonnull while it is actually null... but that's an _Unsafe lie.
3. applyNullabilityToType (:174-179) rebuilds via getAttributedType(BaseTy,BaseTy)
   after desugaring nullability — does it preserve the just-added owned/arrayelem
   local quals through the attributed wrapper? If owned drops, round-trip breaks.

## checkTakeFromRawArgumentShape / handleBSCRawTransferBuiltin (SemaChecking.cpp:222-271) — safe-zone gate probe
**Invariant**: `__take_from_raw(raw)` FORGES `T*_Owned` from a raw pointer (arg must
be raw: not owned/borrow/arrayelem/fnptr, :226); `__move_to_raw(owned)` strips
owned→raw. Manufacturing ownership is inherently unsafe → must be _Unsafe-gated,
else _Safe code can forge/duplicate ownership.
**Peers**: `checkBSCRawTransferBuiltinCommon` (the common gate at :2196 — may hold
the safe-zone check), safe_malloc/safe_free (library primitives built on these).
**Candidates**:
1. **`__take_from_raw` callable in `_Safe` → forge/duplicate ownership — probing**.
   `raw` is a plain (copyable) pointer; two `__take_from_raw(raw)` → two _Owned to
   the same address → double-free. HIGH if _Safe-callable.
2. result-type const/nullability handling (:248-269). UNPROBED.
3. shape check accepts a raw obtained via an odd cast. UNPROBED.

## Ownership-STATE across raw-transfer (2026-06-08) — SOUND (complements qualifier probe)
The 2026-05-30 probe covered QUALIFIER round-trip (clean). Ownership-STATE now confirmed:
`__move_to_raw(p)` CONSUMES the owned — `void *raw=__move_to_raw(p); ... =p;` → "use of moved
value: p" (use-after-move caught, even in an _Unsafe fn — move-tracking runs there too).
`__take_from_raw` creates a leak-tracked owned (exercised by safe_malloc:46 + owned leak detection).
So the owned↔raw boundary is sound in BOTH dimensions (qualifiers + ownership-state). No defect.
