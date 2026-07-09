# SemaChecking.cpp — `_ArrayElem` raw-transfer builtins (move/take array<->raw)

Covers the `_Owned _ArrayElem` ownership/borrow tracking chain (commits d568b2d,
de72c2e, e79ba0f, 6974bf7). Hunt steered 2026-05-30 (bsc-explorer, Mode 2).

## The array idiom (from `libcbs/.../bishengc_safety.hbs`)

- alloc: `int *_Owned _ArrayElem p = safe_malloc_array(n, init);`
  (body: `__take_array_from_raw(malloc(...))`)
- index: `p[i] = x; int v = p[i];`
- free:  `safe_free_array((void *_Owned _ArrayElem)p);`
  (body: `free(__move_array_to_raw(p));`)
- scalar peers: `safe_malloc`/`safe_free`, `__take_from_raw`/`__move_to_raw`.

## checkMoveToRawArgumentShape (SemaChecking.cpp:198-219) — SOUND

**Invariant**: `__move_to_raw` requires owned-non-arrayelem; `__move_array_to_raw`
requires owned-AND-arrayelem; each diagnoses the wrong-variant misuse with a
"use the other builtin" note.
**Peers**: `checkTakeFromRawArgumentShape` (mirror, take side).
**Candidates** (all checked sound):
1. arrayelem/scalar swap — both reject with note. SOUND.
2. non-owned arg — `IsNotOwnedPtr` rejects both. SOUND.

## checkTakeFromRawArgumentShape (SemaChecking.cpp:222-242) — SOUND

**Invariant**: take-from-raw arg must be a *raw* pointer (not owned/borrow/
arrayelem, not fn-ptr); array vs scalar variant only changes the diag text.
**Peers**: move side above; `handleBSCRawTransferBuiltin` (result type).

## handleBSCRawTransferBuiltin (SemaChecking.cpp:245-271) — result type — SOUND on surface

**Invariant**: result = arg type with owned/borrow/arrayelem stripped, then
re-add owned (take_from_raw) or owned+arrayelem (take_array_from_raw); copy
src nullability onto result. move-to-raw side strips all three -> raw.
**Peers**: TypeBSC.cpp:152-175 (the `_ArrayElem` asymmetry rule on safe/unsafe
cast sides), SemaBSCSafeZone.cpp:368-382 (`_Borrow _ArrayElem`->plain reborrow).
**Candidates** (UNPROBED, lower priority — would need a runtime FN):
1. pointee const/volatile preservation across the strip/re-add at :248-265 —
   only place quals are recomputed; not probed for cv-laundering.
2. nullability copy at :266-269 when arg is `_Nullable` raw and result owned —
   not separated from safe_malloc_array's own null handling.

## Runtime move/free/leak tracking — 8 control-flow shapes ALL SOUND (2026-05-30)

The BSCOwnership move/leak lattice treats `_Owned _ArrayElem` exactly like any
`_Owned` pointer; the `(void *_Owned _ArrayElem)p` cast inside `safe_free_array`
is correctly accounted as a MOVE. Verified via vg_probe (checker-accepted oracle):

| shape | verdict | why sound |
|-------|---------|-----------|
| conditional free on one branch | REJECT leak | per-path owned-state merge |
| use array after safe_free_array | REJECT use-of-moved | cast = move |
| cast away `_ArrayElem` to plain owned + free | REJECT incompatible-cast | TypeBSC asymmetry rule |
| overwrite p with new array (no free) | REJECT assign-to-owned | reassign-over-owned |
| free inside loop body | REJECT moved-cast on back-edge | loop merge sees Moved |
| init `_Borrow _ArrayElem` from owned | REJECT forbidden-conv | no implicit owned->borrow |
| element borrow then leak array | REJECT leak | borrow doesn't consume owner |

The "known sound this session" list (element-borrow path collapse, `a++` while
borrowed, owned-`++` rejected) is corroborated. **No array-specific move/free/
leak FN found** on the reachable in-scope surface. Distinct from F75 (owned
struct *field* merge double-free — fields, not array elements).
