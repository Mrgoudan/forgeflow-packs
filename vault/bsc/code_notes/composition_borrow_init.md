# composition_borrow_init.md — borrow-checker × init-analysis composition

Cross-analyzer surface: `&_Mut x` / `&_Const x` makes a borrow `b` of `x`.
- **Init analysis** (`BSCIRInitAnalysis.cpp`, BSCIR-based dataflow) tracks whether `x` is Initialized.
- **Borrow checker** (`BSCBorrowChecker.cpp`, AST-CFG-based) tracks the borrow's lifetime/loans.

The question: do they compose soundly when a borrow's referent's init-state matters?

## The two analyzers' handling of borrow create/deref (from prior notes)

- `BSCIRInitAnalysis.md:86-90`: `&_Mut x` where x uninit → **CORRECTLY rejected** ("use of uninitialized value"). `*b = val` where b borrows uninit → "blocked at borrow creation". `(*b).y = X` where b borrows fully-init → standard tracking.
- That means: **taking `&_Mut x` of an uninit `x` is itself flagged as a use-of-uninit** (the borrow-create reads x's init-state). So the simple `int x; int *_Borrow b = &_Mut x;` is rejected at the `&_Mut x` site.

## The composition gap to hunt

The init analysis flags `&_Mut x` of uninit x. But what about:
1. **WRITE-through-borrow initializes referent?** `int x; int *_Borrow b = &_Mut x; *b = 5; use(x);` — IF `&_Mut x` is accepted (because the borrow is a WRITE-intent that will initialize), does `*b = 5` mark `x` Init so `use(x)` is OK? If init analysis does NOT propagate the write-through-borrow to `x`, that's an FP (reject use(x)). If it OVER-credits (a partial/no write still marks x Init), that's an FN. **But the prior note says `&_Mut x` of uninit is rejected outright** — so this path may be shape-blocked. Need to re-confirm on current binary.
2. **READ through borrow of uninit referent ACCEPTED → garbage read.** If there's any way to create `b = &_Mut x` while `x` is uninit (e.g. the `&_Mut` is gated but `&_Const` is not, or the borrow is created in a context where init analysis loses the connection), then `use(*b)` reads garbage. Need: a borrow whose referent the init analysis believes is init (or doesn't track) but is actually uninit.
3. **Borrow of a partially-init struct field.** `struct S s; s.a=1; int *_Borrow b = &_Mut s.b; use(*b);` — `s.b` uninit. Is `&_Mut s.b` flagged? If only the whole-struct or whole-field granularity is checked, the field-level uninit may slip.
4. **Init credited via borrow but referent is a DIFFERENT object** — write through `b` (a borrow of `y`) doesn't init `x`; if the analyzer conflates the borrow's referent.

## Candidates (ranked) — ALL PROBED-SOUND 2026-05-30 (R3E4), no FN

**KEY STRUCTURAL FACT (re-confirmed on binary 28656aa9):** the **borrow-create site**
(`&_Mut x` / `&_Const x`) is itself treated by the init analysis as a **USE of the referent's
init-state**, fired at the correct granularity *before* any read-through-borrow can run. So the
borrow-create is the init FIREWALL — a read-through-borrow of an uninit referent is unreachable.

1. **Borrow-create of uninit + write-through-borrow init-credit** — **PROBED-SOUND**.
   `int x; b=&_Mut x; *b=5; use(x)` → `&_Mut x` REJECTED (use-of-uninit). The write-through `*b=5`
   does NOT credit `x` init (the later `use(x)` is also flagged), but that's an FP masked by the
   create-site error, NOT an FN. No accept of an uninit read. (shapes B1/B3/B4)
2. **`&_Const x` of uninit x** — **PROBED-SOUND**. Symmetric to `&_Mut`: REJECTED at `&_Const x`
   (use-of-uninit). (shape B2)
3. **Borrow of uninit struct FIELD `&_Mut s.b`** — **PROBED-SOUND**. Caught FIELD-granular
   ("use of uninitialized value: `s.b`"); init field control accepted; whole-struct borrow `&_Mut s`
   of a partial struct REJECTED requiring WHOLE struct init; nested `&_Mut o.in` caught at
   nested-field granularity; MaybeInit field (init on one if-branch) caught ("possibly
   uninitialized"). (shapes C3a/C3b/C4/D3/H1)
4. **Write-through-borrow credits init at WRONG granularity** — **PROBED-SOUND**.
   `&_Mut s.a; *b=9; use(s.b)` (b still uninit) → REJECTED on `s.b`; write through field `a` does
   NOT launder field `b`. (shape D2)

**Additional move×borrow interleaving (steering candidate b) — ALL SOUND:**
- `&_Mut s.p[c]` / `&_Mut pa[c]` of uninit pointer (F83's Index projection on the Ref/AddressOf
  side) → REJECTED. **DISTINCT from F83** — F83 is the dest-side WRITE `s.p[c]=v` only; the Ref form
  IS checked. (shapes E1/E2)
- borrow of a MOVED owned (`consume(p); &_Const *p`) → REJECTED "use of moved value". (shape G1, I1)
- move/free a referent while borrowed (`b=&_Const *p; consume(p)`) → REJECTED "cannot move out of `p`
  because it is borrowed". (shape G2)
- write a field while it is borrowed (`b=&_Const s.b; s.b=99`) → REJECTED "cannot assign to `s.b`
  because it is borrowed". (shape I2)

**VERDICT: composition is SOUND.** No vg run was needed — every probe was REJECTED at compile, so no
runtime garbage read was reachable. Ledger: `/tmp/probed_R3E4.md`. SATURATED-SOUND @ 28656aa9 for the
"read/write through a borrow of an uninit/moved/MaybeInit referent" question. Reopen-if a commit
touches the borrow-create init-use lowering (BSCIRBuilder Ref/AddressOf → Place init check) or the
`&_Mut`/`&_Const` Sema gate.

## borrow-of-narrowed-nullable-pointee (borrow × nullability) — probe 2026-06-24
**Invariant**: after `if(p!=nullptr)` narrows a `_Borrow _Nullable` p to nonnull, `&_Mut *p` (borrow the
pointee) must be allowed (p is nonnull in-branch) and the loan tracked; outside the branch *p is forbidden.
**Peers**: getExprPathNullability, &_Mut build path, narrowing-flow.
**Candidates**: 1. **`if(p){ int *_Borrow q=&_Mut *p; *q=1; }` — accepted+tracked (sound) vs FP (narrowing not
applied to borrow-create) vs FN** UNPROBED ⭐. 2. borrow *p OUTSIDE the null-check (must reject). 3. &_Const.
