# SemaBSCOverload.cpp + Chain-L selection helpers

Source: `clang/lib/Sema/BSC/SemaBSCOverload.cpp` (only `CheckIsUnsafeOverloadCall`
in scope; rest is operator-overload, OOS). The real Chain-L logic lives in
`SemaBSCSafeZone.cpp` selection helpers + the two callers (SemaExpr.cpp direct-call,
IsSafeFunctionPointerTypeCast fnptr-assign).

## CheckIsUnsafeOverloadCall (SemaBSCOverload.cpp:205-215) — read 2026-05-30
**Invariant**: in a safe zone, an OPERATOR-overload call resolves to a `_Safe`
overload; if the resolved `Fn->getType()` is not `SZ_Safe`, diagnose. Gates on
`checkFunctionProtoType(SZ_Safe)` of the RESOLVED fn type.
**Scope**: only reached for OPERATOR overloads (called from SemaOverload.cpp
BuildOverloadedCallExpr paths). Operator overloading is OUT OF SCOPE for this task.
**Candidates**: none in-scope (operator-overload only).

## Chain-L selection helpers (SemaBSCSafeZone.cpp) — read 2026-05-30

### SelectDeclForHeterogeneousRedecl (:236-285)
**Invariant**: among all redecls of a function with BOTH `_Safe` and `_Unsafe`
decls, pick the right one for the context. Safe context: ONLY a SafeDecl that
passes `CheckConstraints`; else nullptr (error). Unsafe context: prefer a passing
SafeDecl, else a passing UnsafeDecl, else nullptr. If not heterogeneous
(`SafeDecls.empty() || UnsafeDecls.empty()`) returns CurrentDecl unchanged.
**Callers (2)**:
- SemaExpr.cpp:7427 — DIRECT CALL. `CheckConstraints = IsCallAssignmentCompatible`
  (arg-type match, AllowImplicitConversions=TRUE). Context = `IsInSafeZone()`
  (lexical). After select, `Fn` rebuilt with `BestMatch->getType()`; SZ gate at
  :7765 then checks `Fn->getType()`.
- SemaBSCSafeZone.cpp:561 (via SelectFunctionDeclForPointerAssignment) — FNPTR
  ASSIGN. `CheckConstraints = DoesFunctionPointerSatisfyConstraints` (strict,
  AllowImplicitConversions=FALSE). Context = DestFnPtr's `getFunSafeZoneSpecifier`
  == SZ_Safe (the TARGET fnptr type, NOT the lexical zone!).

### CheckCallAssignmentConstraints / IsCallAssignmentCompatible (:314-343)
**Invariant**: each arg satisfies its param's BSC qualifiers. Uses
`DoPointerTypesSatisfyAssignmentConstraints` (AllowImplicit=TRUE).
**Hole candidate**: `AreBSCPointerQualifiersCompatible` (:369) is OUTER-LEVEL
only (isOwnedQualified/isBorrowQualified/isArrayElemQualified at outer ptr) — same
F76/F77 family. Nested-owned param differences invisible to the SELECTION gate.

### DoPointerTypesSatisfyAssignmentConstraintsImpl (:395-492) — F76 home
**Invariant**: pointee strict compare. Lines 482-484 use
`getCanonicalType().getUnqualifiedType()` → drops nested BSC quals. THIS IS F76.
Any fnptr-assign nested-qual finding here FOLDS into F76.

### Candidates (ranked) for a DISTINCT root (not F76/F77)
1. **Context-source asymmetry in the two callers** — direct call uses LEXICAL
   `IsInSafeZone()`; fnptr-assign uses the DESTINATION fnptr's SZ spec. If a
   `_Safe`-typed fnptr is built in an UNSAFE lexical zone from a heterogeneous fn,
   the fnptr path uses `IsInSafeContext=true` (dest is safe) and demands a SafeDecl
   — consistent. But the OPPOSITE: an UNSAFE-typed fnptr built in a SAFE lexical
   zone uses `IsInSafeContext=false` → it will PREFER the safe decl but may FALL
   BACK to the unsafe decl (line 278-281) and bind it — producing, in a SAFE
   lexical zone, a fnptr referencing the UNSAFE overload's body. Whether the later
   call of that fnptr is gated is the question. TOP CANDIDATE.
2. Selection returns CurrentDecl for non-heterogeneous (only-_Unsafe) source; the
   downstream SZ-mismatch check (:626) must still reject unsafe->safe. Probe the
   only-_Unsafe x safe-fnptr cell.
3. Direct-call selection constraint is outer-only (nested-owned param) — but this
   folds into the F76/F77 outer-only family if the bug is "qualifier compare too
   shallow." Only distinct if SELECTION (not compare) picks a wrong-contract overload.

## VERDICT 2026-05-30 (Chain-L traced, bsc-explorer): SOUND at outer-qual level — NO new root
- Candidate #1 (context-source asymmetry): SOUND. Direct-call uses lexical
  `IsInSafeZone()`; fnptr-assign uses dest-fnptr SZ. Both consistent: safe-dest demands
  a SafeDecl; unsafe-dest prefers safe then falls back to unsafe; the SZ-mismatch check
  (SemaBSCSafeZone.cpp:626) rejects unsafe→safe regardless of which view selection picked.
- Candidate #2 (only-_Unsafe x safe-fnptr): SOUND. `safe_fp = only_unsafe_fn` REJECTED in
  both zones (selection returns CurrentDecl unchanged; downstream :626 rejects).
- Candidate #3 (selection picks wrong-contract overload): SOUND at OUTER level — every
  owned/borrow/raw mismatch on param & return is caught by `AreBSCPointerQualifiersCompatible`
  before selection commits. Wrappers (paren/ternary/`*&`) that defeat selection's DRE detection
  make RHSFuncType = the SAFE-view default-DRE type → MORE conservative, not a bypass.
- NESTED-qualifier hole (`int*_Owned*` safe view vs `int**` dest fnptr) → silently ACCEPTED
  → **FOLDED-F76** (routes through `DoPointerTypesSatisfyAssignmentConstraintsImpl:482`
  canonical-unqualified pointee compare — F76's exact root; selection is a NEW CALLER, not a
  new root). Repro /tmp/explorer_probe.afhR3m.cbs.
- "unsafe-view lies" (unsafe view raw, body frees): SHAPE-REJECTED — feeding an owned alias to
  the raw fnptr needs `__move_to_raw`; the raw-cast rule gates the only path to a double-free.
- Mark CheckIsUnsafeOverloadCall: OOS (operator-overload only).
- Sub-chain worth registering: "heterogeneous-selection caller of
  DoPointerTypesSatisfyAssignmentConstraintsImpl" — a future F76 fix must verify it also closes
  the SelectFunctionDeclForPointerAssignment path (main thread: add_chain.sh).
