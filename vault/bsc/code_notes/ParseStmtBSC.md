# ParseStmtBSC.cpp — BSC statement/block parser (77 lines)

- **ParseSafeStatement (56)**: parses `_Safe`/`_Unsafe` statement modifier — saves scope safe-zone, sets new spec, ParseStatement, ActOnSafeStmt, restores (74, all-paths). Invalid sub-stmt → NullStmt recovery (70-71). Nested `_Safe _Unsafe stmt` recurses (harmless).
- **CheckStmtTokInSafeZone (21)**: parse-time safe-zone gate — ONLY rejects `tok::kw_asm` ("asm statement"); all other safe-zone restrictions (raw deref, mutable global, etc.) are enforced at Sema, not parse. No parse-time bug surface beyond asm (covered).
- **getCurScopeSafeZoneInfo/setCurScopeSafeZoneInfo (33/42)**: scope safe-zone get/set helpers.
- **PROBED-no-bug-surface** (like SemaStmtBSC): pure parse + safe-zone scope plumbing; soundness enforcement is at Sema. File-level BSC source coverage now COMPLETE.
