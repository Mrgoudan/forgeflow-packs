# Tier 2/3 data model: how hunt, fix, and review share one brain

The engine gives us the tables (findings, transitions, coverage, readings,
implications, patterns, lessons, chains, embeddings, runs) and the event
bus. This doc fixes WHO writes each table, WHO reads it, and WHICH events
stitch the workflows. Companion to DESIGN.md / HUNT.md; supersedes their
data sketches where they differ.

## 1. The one shared entity

`findings` is the only cross-workflow state. Hunt, review, and humans
produce findings (`source` column says who); triage routes them; fix
consumes them; review re-checks them; learn distills them. Every state
change goes through `record_transition` — the transition IS the
integration event.

```
bughunt ────┐
PR review ──┼──> findings ──triaged──> autofix ──pr_open──> self-review ──merged──┐
human ──────┘        ▲                                                            │
                     └────────────── variant hunt / learn / patterns <────────────┘
```

## 2. Table ownership (single writer per purpose)

| table | writer | readers | notes |
|---|---|---|---|
| findings, transitions | any workflow via db.upsert_finding / db.transition | all | the bus |
| coverage | hunt sweeps | hunt (skip swept), review (staleness flag) | (object, workflow, sha, probe_rev) -> clean/findings:n |
| readings | hunt/fix agents | review + fix context | sha-pinned; stale = "re-verify", never fact |
| implications | triage/fix | review (defect history), hunt (hot spots) | finding <-> object, role root_cause/touched_by_fix/witness |
| patterns | learn only | review (lens + grep rules), hunt (targeting) | escapes++ when hunt finds what review passed |
| lessons | learn only (append) | prompt assembly, per task kind | |
| chains | hunt | review (hop invariants), hunt | curated call paths, sha-pinned |
| embeddings | model.embed steps | retrieval provider | objects AND findings (see §5) |
| runs, egress | engine choke points | audit, provenance | |

## 3. Event wiring (the whole integration, as config)

```yaml
autofix:   consumes: [finding.triaged, comment.fix_request]
review:    consumes: [pr.updated, finding.pr_open]   # external + our own PRs
bughunt:   consumes: [finding.merged, hunt.round_requested]
learn:     consumes: [finding.merged, finding.rejected]
pr_report: consumes: [review.completed]
```

- fix -> self-review: a fix PR gets the same lens as an external PR
  before any human does.
- merged fix -> variant hunt: every fixed defect seeds a sweep for its
  siblings.
- review escape: hunt files a finding in code coverage says review
  passed -> patterns.escapes++ (the lens-quality metric).

## 4. Review context assembly (what the lens gets to see)

Ordered by evidence-per-token. 1-3 are NO-AI and run even with every
model down; 4-8 are context providers registered by this pack (engine
mechanism: @context_provider, declared per step, pinned by prompt_sha).

1. machine checks on the diff: patterns.grep_rule hits + probe sweep
   head-vs-base flips + build result — these FILE findings, not hints
2. defect history: implications ⋈ findings for touched objects
   ("this fn was root cause of F41, merged in PR#12")
3. coverage staleness: touched objects never swept at a recent sha
   -> flag + enqueue hunt sweep
4. readings of touched objects (fresh = fact, stale = re-verify)
5. chain hop invariants when the diff edits a hop
6. similar past findings via retrieval (query = diff symbols + titles)
7. fix provenance on self-review: finding detail + evidence + pinned run
   (the lens verifies exactly what the fix claimed)
8. lessons WHERE task_kind = review

Provider names to implement (pack code, no engine changes):
`history`, `coverage_gaps`, `chain_invariants`, `provenance`, `lessons`.
(`retrieval` and `notes` already exist in the engine.)

## 5. Finding similarity (dedup / variants)

embeddings is keyed to code_objects; findings get a pseudo object
(kind='finding', path='finding:<key>') — kind is unconstrained TEXT, no
migration. Near-dup detection PROPOSES a verification task
(`dedup.check_requested`); nothing auto-merges. Local-model outputs are
claims — they shape attention, never gate transitions.

## 6. Decisions locked here

- One findings table for all three systems; `source` + `pattern` columns
  carry lineage. No per-workflow finding tables, ever.
- Enrichment tables are advisory: losing readings/embeddings/coverage
  degrades yield, never correctness (rebuildable from repo + findings).
- Review must stay useful with zero AI: items 1-3 above are the no-AI
  core and post findings through the same choke points.
- Cross-workflow reads happen ONLY via context providers (declared,
  pinned) or staged writes — no block queries the db ad hoc.
