-- hunt pack schema: the campaign's exploration data model. The engine core is
-- domain-agnostic (tasks/items/transitions/events/...); these tables are the
-- hunt machinery (region-lease surface, the method bandit, call-chains, the
-- sweep coverage ledger). The engine applies this after its core schema
-- (project.yaml `schema:`); FKs point at core tables (tasks, code_objects).

CREATE TABLE IF NOT EXISTS methods (               -- the detection-method bench
    id            TEXT PRIMARY KEY,
    description   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'candidate', -- candidate | active | exhausted
    trials        INTEGER NOT NULL DEFAULT 0,
    verified_yield INTEGER NOT NULL DEFAULT 0,     -- items that passed the repro gate
    last_used_round INTEGER
);

CREATE TABLE IF NOT EXISTS regions (               -- explore surface map
    id            TEXT PRIMARY KEY,      -- a source subsystem path prefix
    repo          TEXT NOT NULL,
    dry_streak    INTEGER NOT NULL DEFAULT 0,      -- consecutive no-new explores
    cooldown_until_round INTEGER,
    leased_by_task INTEGER REFERENCES tasks(id)    -- disjointness: one explorer per region
);

CREATE TABLE IF NOT EXISTS chains (                -- curated traced call-paths
    id            TEXT PRIMARY KEY,
    repo          TEXT NOT NULL,
    sha           TEXT NOT NULL,         -- validity pin; hops drift with code
    nodes         TEXT NOT NULL,         -- JSON: [{path, line, symbol}, ...]
    hop_invariants TEXT NOT NULL,        -- JSON: per-hop promise + rank
    yields        TEXT,                  -- JSON: item keys this chain produced
    status        TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS coverage (              -- sweep ledger: where we have looked
    object_id     INTEGER NOT NULL REFERENCES code_objects(id),
    workflow      TEXT NOT NULL,
    sha           TEXT NOT NULL,         -- tree state when swept
    probe_rev     TEXT,                  -- checker version used
    outcome       TEXT NOT NULL,         -- clean | items:<n>
    swept_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (object_id, workflow, sha)
);
