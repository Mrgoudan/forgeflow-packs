# clang/lib/Sema/BSC/SemaStmtBSC.cpp — Function Notes (read 2026-05-29)

Tiny file (43 lines). **No bug surface.**
- `ActOnSafeStmt` / `ActOnSafeExpr` (:33,:39): pure AST-node construction (`new SafeStmt/SafeExpr`). No logic to bug; SafeStmt/SafeExpr SEMANTICS are checked elsewhere (DiagnoseInvalidUnaryExprInSafeZone F68, IsSafeConversion F51/F71, getExprPathNullability SafeExpr-strip, etc. — all covered).
- `CheckBSCConstexprCondition` (:25): constexpr-if condition type gate (isBSCCalculatedTypeInCompileTime + CheckCXXBooleanCondition). constexpr is OOS-adjacent (not in _Owned/_Borrow/_Safe/init/nullability scope). Simple type check; no probe.

## BSC source file coverage (2026-05-29)
Analysis/BSC: BSCBorrowChecker, BSCIRBuilder, BSCIRInitAnalysis, BSCNullabilityCheck,
BSCNullCheckInfo, BSCOwnership = covered. BSCIR.cpp/BSCIRDump.cpp = IR data-structures/debug-dump (infra, low bug-surface).
Sema/BSC: SemaBSCOwnership, SemaBSCSafeZone, SemaDeclBSC, SemaStmtBSC(this) = covered.
  REMAINING UNREAD IN-SCOPE: DeclSpecBSC.cpp (declspec parse), SemaBSCOverload.cpp (overload res — mixed-mode probed clean F-cycle).
  OOS: SemaBSCCoroutine, SemaBSCDestructor (_Owned struct dtor), SemaBSCOwnedStruct, SemaBSCTrait, SemaTemplateInstantiateDeclBSC (generics).

## SemaBSCOverload.cpp (read 2026-05-29) — no in-scope bug surface
- Mostly operator-overloading (getOperatorKindByDeclarator, CheckBSCOverloadedOperatorDeclaration, OperatorUses matrix) — tangential to the _Owned/_Borrow/_Safe/init/nullability core scope.
- `CheckIsUnsafeOverloadCall` (:205-215): SOUND by inspection — in a safe zone, `!Fn->getType()->checkFunctionProtoType(SZ_Safe)` → `err_unsafe_action << "overload _Unsafe function"`. Correctly rejects calling an unsafe overloaded operator from safe code. (Nested-fnptr-safe-variance for operators would fold into the IsSafeFunctionPointerTypeCast/F-class.)
- **In-scope BSC Sema/Analysis file inventory now COMPLETE**: only DeclSpecBSC.cpp (parser-level declspec parsing of _Owned/_Borrow/_Safe qualifiers) remains unread — low semantic-bug surface (parsing, not analysis). All analysis/checking logic files read/assessed.
