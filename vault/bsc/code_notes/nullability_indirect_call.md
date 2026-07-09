# nullability_indirect_call — `_Nullable` function pointer as CALLEE

Source: `clang/lib/Analysis/BSC/BSCNullabilityCheck.cpp`, `TransferFunctions::VisitCallExpr` (:654-668).

## Context / dedup

The bare-call form of a `_Nullable` callee (`fp()`, `s.fp()`) bypassing the
null-deref check is **already filed as F31** (HIGH, IJ... callee-position not
visited). Root cause: `VisitCallExpr` only loops `getDirectCallee()` params for
the ARGUMENT check; it never calls `getExprPathNullability(CE->getCallee())`.
F28/F49 are the indirect-ARG variants; F29 is the fnptr-assignment-variance
variant. F31's fix surface is "visit the callee position".

So: the PRIMARY hint hypothesis (bare `fp()` of `_Nullable` callee accepted →
SIGSEGV) is a **FOLD of F31**. Do not re-file.

## Distinct angles being hunted (NOT folds of F31)

F31 is a false-NEGATIVE on the BARE-call form. The explicit-deref callee form
`(*fp)()` is, per F31's note, "correctly fires `nullable pointer cannot be
dereferenced`". That correct-fire path goes through `VisitUnaryOperator`
(UO_Deref on the `*fp` subexpr), NOT through VisitCallExpr.

### `VisitCallExpr` (:654-668) — PROBED 2026-06-25 = F31 (FILED HIGH, still reproduces)
Nullable fnptr callee `fp()` not checked (CE->getCallee() nullability never inspected) → runtime SIGSEGV. Re-validated 2026-06-25: compile rc=0, runtime exit=139. Same VisitCallExpr indirect-call hole family as F28 (args).
**Invariant**: a call must reject a `_Nullable` callee (a call dereferences the
fnptr). Also, after `if(fp)` narrows the fnptr to NonNull, the call must be
ACCEPTED (no false positive).
**Peers**: VisitUnaryOperator (:674, UO_Deref handles `(*fp)()`'s deref),
getExprPathNullability (:312, callee classification + narrowing lookup).
**Candidates**:
1. **`(*fp)()` false POSITIVE after `if(fp)` narrowing** — DISTINCT from F31.
   — **PROBED-SOUND 2026-05-30 (R4E5)**: `(*fp)()` after `if(fp)` ACCEPTS (no
   diag); narrowing IS correctly applied to the deref-callee position. Not a bug.
2. **bare `fp()` of `_Nullable` LOCAL var (not field)** — same VisitCallExpr
   callee-skip. — **PROBED-FOLDED-F31 2026-05-30**: bare `fp()` ACCEPTS (false
   neg) on current binary; same root as F31.
3. **`if(fp) fp();` bare-call after narrowing** — FOLD-F31. Skip.

## RESOLUTION (2026-05-30, R4E5 — NO-NEW)

Every callee-nullability shape probed is either FOLD-F31 (bare `fp()`/`s.fp()`,
the only false-negative gap, already filed) or SOUND. The explicit-deref-callee
path (UO_Deref via `VisitUnaryOperator`) correctly REJECTS the unguarded form
across ALL shapes tested — local, field, ternary-arm, double-deref `(**pp)()`,
call-result `(*get())()` — and correctly ACCEPTS after `if(fp)` narrowing. The
hint's "narrowing that doesn't apply to the callee" angle is SOUND (candidate 1).

**No distinct root cause.** Surface SATURATED @ 28656aa9. The single F31 fix
(visit `CE->getCallee()` in `VisitCallExpr`) closes the bare-call gap; there is
no second gap behind it. Reopen-if `VisitCallExpr` gains a callee-visit hook
(to verify it doesn't introduce a narrowing FP) or `VisitUnaryOperator`'s
UO_Deref path changes.

Probe ledger: /tmp/probed_R4E5.md. Probes (all in /tmp): explorer_probe.jkslNj
(A), .NOAZ6Y (B), .6Oxn6u (C), .ENUbzn (D), .YK2rP1 (E), .QM6zuN (F), .2nkD8g
(G/F31-liveness), .B32zx8 (H).
