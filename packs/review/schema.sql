-- review pack schema: the finding-classification data model layered on the
-- generic engine core. `patterns` = graduated defect classes (a prompt lens +
-- a machine-checkable rule); `lessons` = review guidance injected into prompts;
-- `implications` = item <-> code mapping. Applied via project.yaml `schema:`;
-- FKs point at core tables (items, code_objects).

CREATE TABLE IF NOT EXISTS patterns (              -- graduated from items.pattern
    id            TEXT PRIMARY KEY,
    description   TEXT NOT NULL,
    review_lens   TEXT,                  -- text injected into review prompts
    grep_rule     TEXT,                  -- machine-checkable rule: a no-AI finder
    status        TEXT NOT NULL DEFAULT 'active',  -- active | retired
    escapes       INTEGER NOT NULL DEFAULT 0,      -- found later after review missed it
    catches       INTEGER NOT NULL DEFAULT 0       -- caught at review time
);

CREATE TABLE IF NOT EXISTS lessons (
    id            INTEGER PRIMARY KEY,
    task_kind     TEXT NOT NULL,         -- which task kinds this lesson applies to
    trigger       TEXT NOT NULL,         -- what situation activates it
    rule          TEXT NOT NULL,         -- the instruction injected into prompts
    provenance    TEXT,                  -- PR/incident it came from
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS implications (          -- item <-> code mapping
    item_id       INTEGER NOT NULL REFERENCES items(id),
    object_id     INTEGER NOT NULL REFERENCES code_objects(id),
    role          TEXT NOT NULL,         -- root_cause | touched_by_fix | witness
    PRIMARY KEY (item_id, object_id, role)
);
