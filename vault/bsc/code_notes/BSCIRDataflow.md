# BSCIRDataflow.h — generic forward/backward dataflow framework on BSCIR

`clang/include/clang/Analysis/Analyses/BSC/BSCIRDataflow.h` (298 lines). The single
worklist template that the per-analysis BSCIR dataflows (init analysis, etc.) build on.
Subclasses implement `entryState` / `transferStatement` / `transferTerminator` (edge
refinement = narrowing) / `merge` (join).

## runForwardAnalysis (line 131) — worklist solver
- **Invariant**: at fixpoint, `Entry[B] = join over preds P of transferTerminator(Exit[P], B)`, and `Exit[B] = transfer(Entry[B])`; monotone ⇒ least fixpoint = sound over-approximation.
- **Structure**: entry block's state pre-set to `entryState(B)` (142); each visit RECOMPUTES `Entry[B]` fresh as the join of current pred exits (169-174), then `Exit[B]` is ACCUMULATED via `merge(State, OldExit)` (193). Fresh-entry + accumulated-exit converges to the same fixpoint for monotone frameworks (accumulated exit never decreases ⇒ pred exits monotone ⇒ fresh entry recompute monotone).
- **Candidates**:
  1. **PROBED-SOUND 2026-06-29**: line 159 `if (BId != EntryId)` SKIPS the predecessor-merge for the entry block — so if block 0 were itself a loop header (back-edge target), the back-edge state would be dropped → FN. TEST: a function STARTING with a loop over a param-`_Owned` (`_Safe void g(int*_Owned p,int n){ while(n>0){consume(p); n=n-1;} }`) correctly REJECTS "use of moved value: p" on the 2nd iteration + leak on zero-iter path. So BSCIR emits a DEDICATED pred-less entry block (loop header is a separate block) — the skip is safe. CONFIRMED via the INIT analysis (the ACTUAL framework user — BSCOwnership uses its own worklist, only BSCIRInitAnalysis.cpp:1964 calls runForwardAnalysis): leading-loop read-before-init `int sum; while(n>0){sum=sum+n;...}` correctly REJECTED "use of possibly uninitialized value: sum"; init-only-in-loop used-after REJECTED maybe-uninit; valid init-before-loop clean. Framework loop-header merge sound. No FN/FP.
  2. **UNPROBED** (likely sound): exit-accumulation (`merge(State,OldExit)`, 193) vs fresh-entry (170) monotonicity — relies on transfer monotonicity; non-monotone transfer could mis-converge, but init/ownership transfers are monotone.
  3. **PROBED-SOUND 2026-06-29**: switch terminator successor-completeness — init-analysis across a switch with NO default (`int x; switch(c){case 1:x=10;break; case 2:x=20;break;} return x;`) correctly REJECTS "use of possibly uninitialized value: x" (the implicit no-match edge to the after-switch block IS a successor carrying x-uninit); with-default control clean. forEachSuccessor includes the implicit fall-out edge. No FN.

## runBackwardAnalysis (line 214) — symmetric; exit blocks = Return/Unreachable (227). Same fresh-recompute/accumulate structure mirrored (successors↔predecessors).
