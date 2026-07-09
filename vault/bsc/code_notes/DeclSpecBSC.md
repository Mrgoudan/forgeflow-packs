# DeclSpecBSC.cpp (clang/lib/Sema/BSC) — 52 lines, parser-level declspec setters

## setFunctionSafeZoneSpecifier (:34-51) — read 2026-06-25
INVARIANT: a function decl carries AT MOST ONE safe-zone specifier; the first _Safe/_Unsafe sets
FS_safe_zone_specified, any subsequent one → err_duplicate_declspec (return true=error). Fail-closed.
PEERS: setFunctionSpecAsync (:22, _Async — coroutine, OUT OF SCOPE).
CANDIDATES (all LOW/sound):
1. conflicting `_Safe _Unsafe` reported as "duplicate" not "conflicting" — COSMETIC (both rejected, fail-closed;
   confirmed _Safe _Unsafe rc=1, _Safe _Safe rc=1). Not a bug.
2. first-wins-then-error recovery could leave the wrong zone set — SOUND: an error IS emitted (compile fails),
   so no silent mis-zoning.
3. else-branch PrevSpec="" for an unexpected enum value — defensive, unreachable (only None/Safe/Unsafe exist).
SOUND. setFunctionSpecAsync OUT OF SCOPE (async/coroutine).
