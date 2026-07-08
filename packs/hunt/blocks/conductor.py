"""The Conductor — HUNT.md's dispatch, as deterministic db functions.

Per HUNT.md the Conductor is NOT an agent: it is the daemon + dispatch
rules, where every decision is a pure function of db state at claim time,
serialized through the single-writer queue so replaying the event log
reproduces the run. These blocks are those rules:

- hunt.seed          seed regions + methods bench from pack config (idempotent)
- hunt.pick_region   lease the next region AND assign this turn's detection
                     method (UCB *dispatch*: counts the pull immediately and
                     records the arm on the task, so concurrent explorers
                     diverge instead of all picking one argmax, and the merge
                     credits the arm actually used — stable under concurrency).
- hunt.merge_explore apply an explorer's VERIFIED result: file finding+pattern,
                     credit the dispatched arm's yield, update dry_streak/
                     cooldown, enqueue the replacement explorer (auto-swap) +
                     an exploiter on a confirm. Retires a spent arm and, at
                     campaign saturation, kicks the Oracle-Scout.
- hunt.merge_scout   apply the Oracle-Scout's proposals: insert new methods
                     (arsenal growth) and reopen the hunt, or end it.

Rounds are a logical clock in watermarks('hunt.round'); merge ticks it.
Explore vs exploit balance is emergent (every confirm spawns one exploiter;
every return spawns one replacement explorer) — not a knob.
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import os
from pathlib import Path

from forgeflow.blocks import block
from forgeflow.util import template

_SRC_GLOBS = ("*.cpp", "*.c", "*.h", "*.cbs")

COOLDOWN_C = 3          # rounds a region cools after dry_streak hits the limit
DRY_LIMIT = 3           # consecutive dry explores -> cooldown
TRIAL_BUDGET = 8        # pulls a method gets before a zero-yield arm is retired
MAX_SCOUT_ROUNDS = 3    # scout invocations per campaign (runaway backstop)
_SUMMARY_CAP = 400      # reading summary (prompt digest) length
_FACTS_CAP = 8000       # reading facts (full note) length


def _round(conn):
    r = conn.execute("SELECT cursor FROM watermarks WHERE scope='hunt.round'").fetchone()
    return int(r["cursor"]) if r else 0


def _tick_round(conn):
    n = _round(conn) + 1
    conn.execute("INSERT INTO watermarks(scope,cursor) VALUES('hunt.round',?)"
                 " ON CONFLICT(scope) DO UPDATE SET cursor=?", (str(n), str(n)))
    return n


@block("hunt.seed", "state", {"ok"},
       accepts_context={"pack"}, required_params={"repo"})
def hunt_seed(ctx, task, prev):
    """Seed the region surface (FILE-level, not folder-level) and the methods
    bench from pack config. Each configured entry is a repo dir that is
    expanded to one region per source FILE — a file's ~dozens of functions is
    a unit the explorer can actually work through and the dry-streak/cooldown
    can meaningfully exhaust. An entry that isn't an existing dir is seeded
    literally (tests / explicit regions). Idempotent (INSERT OR IGNORE)."""
    conn = ctx["_conn"]
    repo = template(ctx["repo"], {})
    for entry in ctx.get("regions") or []:
        d = os.path.join(repo, entry)
        files = []
        if os.path.isdir(d):
            for pat in _SRC_GLOBS:
                files += glob.glob(os.path.join(d, "**", pat), recursive=True)
        if files:
            for f in sorted(os.path.relpath(f, repo) for f in files):
                conn.execute("INSERT OR IGNORE INTO regions(id, repo) VALUES (?,?)",
                             (f, repo))
        else:
            conn.execute("INSERT OR IGNORE INTO regions(id, repo) VALUES (?,?)",
                         (entry, repo))
    for m in ctx.get("methods") or []:
        conn.execute("INSERT OR IGNORE INTO methods(id, description, status)"
                     " VALUES (?,?, 'active')", (m["id"], m.get("description", "")))
    n_r = conn.execute("SELECT count(*) c FROM regions").fetchone()["c"]
    n_m = conn.execute("SELECT count(*) c FROM methods").fetchone()["c"]
    return "ok", {"regions": n_r, "methods": n_m}


@block("hunt.pick_region", "state", {"leased", "saturated"},
       accepts_context={"pack"})
def hunt_pick_region(ctx, task, prev):
    """Lease the next region: never-explored first, then oldest-explored;
    cooling regions excluded; ORDER BY stable keys. Cap = max_explorers
    leased at once. No region eligible -> saturated (campaign may be done).
    Same db state => same choice."""
    conn = ctx["_conn"]
    cap = int(ctx.get("max_explorers", 6))
    rnd = _round(conn)
    # reclaim stale leases: a region held by a task that already reached a
    # terminal state (crashed/failed explore) is not really in-flight. Only
    # a live explore (pending/running/retry_wait/parked) holds its region.
    conn.execute(
        "UPDATE regions SET leased_by_task=NULL WHERE leased_by_task IS NOT NULL"
        " AND leased_by_task NOT IN (SELECT id FROM tasks WHERE"
        " state IN ('pending','running','retry_wait','parked'))")
    leased = conn.execute(
        "SELECT count(*) c FROM regions WHERE leased_by_task IS NOT NULL"
    ).fetchone()["c"]
    if leased >= cap:
        return "saturated", {"reason": "explorer cap %d reached" % cap}
    row = conn.execute(
        "SELECT id FROM regions"
        " WHERE leased_by_task IS NULL"
        "   AND (cooldown_until_round IS NULL OR cooldown_until_round <= ?)"
        " ORDER BY dry_streak ASC, cooldown_until_round IS NOT NULL, id ASC"
        " LIMIT 1", (rnd,)).fetchone()
    if row is None:
        return "saturated", {"reason": "all regions cooling or leased",
                             "round": rnd}
    conn.execute("UPDATE regions SET leased_by_task=? WHERE id=?",
                 (task["id"], row["id"]))
    # dispatch the detection method: count the pull NOW (so the next
    # serialized explorer sees the bump and diverges) and record the arm on
    # the task, so merge credits exactly what this explorer used.
    method = _pick_method(conn)
    if method:
        conn.execute("UPDATE methods SET trials=trials+1, last_used_round=?"
                     " WHERE id=?", (rnd, method))
        conn.execute("UPDATE tasks SET payload=json_set(payload,'$.method',?)"
                     " WHERE id=?", (method, task["id"]))
    return "leased", {"region": row["id"], "method": method, "round": rnd}


def _pick_method(conn):
    """Deterministic UCB bandit over the active methods bench. Score key
    (compared as a tuple, argmax, ties by id):

        (ucb, idle)   ucb  = verified_yield/trials + sqrt(2*ln(round+1)/trials)
                             (untried = +inf, so unbenched methods go first)
                      idle = rounds since last_used_round (recency: among
                             near-equal arms, rotate to the one idle longest)

    Pure function of db state. The CALLER (pick_region, at dispatch) counts
    the pull and records the arm on the task, so this is never re-run to
    'credit' — no re-pick drift under concurrency."""
    rnd = _round(conn) + 1
    best, best_key = None, None
    for m in conn.execute("SELECT id, trials, verified_yield, last_used_round"
                          " FROM methods WHERE status='active' ORDER BY id"):
        t = m["trials"]
        ucb = float("inf") if t == 0 else (
            m["verified_yield"] / t + math.sqrt(2 * math.log(rnd) / t))
        lur = m["last_used_round"]
        idle = rnd if lur is None else max(0, rnd - lur)
        key = (ucb, idle)
        if best_key is None or key > best_key:      # ORDER BY id => ties -> lowest id
            best, best_key = m["id"], key
    return best


def _record_reading(conn, repo, region, note):
    """Persist the explorer's function reading NATIVELY — the exploration end
    of the loop bsc.ingest_notes seeds (readings become 'indistinguishable
    from readings the agent produces itself'). Keyed by (file, content-hash):

      - new file, or the file changed (hash moved) -> INSERT a fresh reading
        and supersede this file's stale campaign notes (the imported 'seed'
        reading is kept as provenance);
      - same code re-read -> UPDATE in place ONLY if the invariant/candidates
        changed (the old note was wrong/refined) — identical re-read is a
        no-op, so re-affirming a function doesn't churn the table.

    Returns True iff a row was written (new or corrected)."""
    invariant = (note.get("invariant") or "").strip()
    if not region or not invariant:
        return False
    obj = (note.get("object") or "").strip()
    summary = ("%s — %s" % (obj, invariant)).strip(" —")[:_SUMMARY_CAP]
    facts = json.dumps(note)[:_FACTS_CAP]
    try:
        sha = hashlib.sha1(Path(os.path.join(repo, region)).read_bytes()).hexdigest()[:12]
    except OSError:
        sha = "hunt"                                     # file gone/unreadable
    row = conn.execute("SELECT id FROM code_objects WHERE repo=? AND path=?"
                       " AND symbol IS NULL", (repo, region)).fetchone()
    if row:
        obj_id = row["id"]
        conn.execute("UPDATE code_objects SET last_seen_sha=? WHERE id=?", (sha, obj_id))
    else:
        obj_id = conn.execute(
            "INSERT INTO code_objects(repo, path, symbol, kind, first_seen_sha,"
            " last_seen_sha) VALUES (?,?,NULL,'file',?,?)", (repo, region, sha, sha)
        ).lastrowid
    ex = conn.execute("SELECT summary, facts FROM readings WHERE object_id=? AND sha=?",
                      (obj_id, sha)).fetchone()
    if ex is None:                                       # new file / changed code
        conn.execute("DELETE FROM readings WHERE object_id=? AND sha NOT IN (?, 'seed')",
                     (obj_id, sha))
        conn.execute("INSERT INTO readings(object_id, run_id, sha, summary, facts)"
                     " VALUES (?, NULL, ?, ?, ?)", (obj_id, sha, summary, facts))
        return True
    if ex["summary"] != summary or ex["facts"] != facts:  # old note was wrong
        conn.execute("UPDATE readings SET summary=?, facts=? WHERE object_id=? AND sha=?",
                     (summary, facts, obj_id, sha))
        return True
    return False                                         # unchanged -> no churn


@block("hunt.merge_explore", "state", {"confirmed", "dry", "done"},
       accepts_context={"pack"}, required_params={"repo"})
def hunt_merge_explore(ctx, task, prev):
    """Apply the explorer's VERIFIED result (the verify step already ran the
    repro against base clang). Pure dispatch:
      confirmed -> finding + pattern rows, method.verified_yield++, region
                   dry_streak reset, release lease, emit pattern_confirmed
                   (spawn exploiter) + explore_requested (auto-swap).
      dry       -> dry_streak++; at DRY_LIMIT set cooldown; release lease;
                   emit explore_requested unless the campaign is saturated.
    Returns 'done' when no region remains to hand off to (campaign end)."""
    conn = ctx["_conn"]
    repo = template(ctx["repo"], {})
    # derive region from the lease (robust — the agent step doesn't pass prev
    # through); the arm is read from the task where dispatch recorded it.
    lr = conn.execute("SELECT id FROM regions WHERE leased_by_task=?",
                      (task["id"],)).fetchone()
    region = lr["id"] if lr else None
    # the arm this explorer actually used, recorded at dispatch (not re-picked
    # — re-picking here would drift as concurrent merges move the stats).
    mr = conn.execute("SELECT json_extract(payload,'$.method') m FROM tasks"
                      " WHERE id=?", (task["id"],)).fetchone()
    method = mr["m"] if mr else None
    verified = (prev or {}).get("verified") or {}
    confirmed = bool(verified.get("confirmed"))
    # close the exploration->readings loop: persist this turn's function
    # reading (on every path — a dry turn still refines the invariant).
    reading_written = _record_reading(conn, repo, region,
                                      (prev or {}).get("note") or {})
    rnd = _tick_round(conn)
    staged, emits = [], []
    # (the trial/pull was counted at dispatch; here we only credit yield.)

    if region is not None:
        conn.execute("UPDATE regions SET leased_by_task=NULL WHERE id=?", (region,))

    if confirmed:
        key = verified["key"]
        staged.append({"op": "upsert_finding", "key": key,
                       "title": verified.get("title", key)[:200],
                       "source": "bughunt", "repo": repo,
                       "detail": json.dumps(verified.get("evidence", {})),
                       "severity": verified.get("severity", "medium"),
                       "pattern": verified.get("pattern")})
        if verified.get("pattern"):
            conn.execute(
                "INSERT OR IGNORE INTO patterns(id, description, grep_rule)"
                " VALUES (?,?,?)",
                (verified["pattern"], verified.get("title", "")[:200],
                 verified.get("grep_rule")))
        if method:
            conn.execute("UPDATE methods SET verified_yield=verified_yield+1"
                         " WHERE id=?", (method,))
        if region is not None:
            conn.execute("UPDATE regions SET dry_streak=0 WHERE id=?", (region,))
        emits.append({"op": "emit_event", "name": "hunt.pattern_confirmed",
                      "payload": {"pattern": verified.get("pattern"),
                                  "finding_key": key, "region": region}})
        outcome = "confirmed"
    else:
        if region is not None:
            conn.execute(
                "UPDATE regions SET dry_streak=dry_streak+1,"
                " cooldown_until_round=CASE WHEN dry_streak+1>=? THEN ? ELSE"
                " cooldown_until_round END WHERE id=?",
                (DRY_LIMIT, rnd + COOLDOWN_C, region))
        outcome = "dry"

    # retire a spent arm: TRIAL_BUDGET pulls with zero yield -> exhausted, so
    # the bandit stops rotating it (prunes scouted duds AND dead seeded arms).
    if method:
        conn.execute(
            "UPDATE methods SET status='exhausted' WHERE id=? AND status='active'"
            " AND verified_yield=0 AND trials>=?", (method, TRIAL_BUDGET))

    # auto-swap: hand off to the next region unless the campaign is saturated
    nxt = conn.execute(
        "SELECT id FROM regions WHERE leased_by_task IS NULL"
        " AND (cooldown_until_round IS NULL OR cooldown_until_round <= ?)"
        " LIMIT 1", (rnd,)).fetchone()
    if nxt is not None:
        emits.append({"op": "emit_event", "name": "hunt.explore_requested",
                      "payload": {"round": rnd}})
        result = {"round": rnd, "region": region, "reading_written": reading_written,
                  "_staged": staged + emits}
        return outcome, result
    # campaign saturated with the current arsenal: kick the Oracle-Scout to
    # invent new methods (it either reopens the hunt or ends it). Keyed by
    # round so concurrent saturators dedup but a later saturation re-scouts.
    emits.append({"op": "emit_event", "name": "hunt.scout_requested",
                  "payload": {"round": rnd}})
    return "done", {"round": rnd, "region": region, "reading_written": reading_written,
                    "_staged": staged + emits, "campaign": "saturated"}


from forgeflow.contract import context_provider
from forgeflow.util import run_cmd


@context_provider("hunt_region")
def _hunt_region(env, task, spec):
    """The region leased to THIS explorer task + its sha-fresh readings and
    touching chains (Mode 1 + Mode 2 inputs). Region is looked up by lease,
    so the explorer knows exactly what disjoint surface it owns."""
    conn = env.conn
    r = conn.execute("SELECT id, repo FROM regions WHERE leased_by_task=?",
                     (task["id"],)).fetchone()
    if r is None:
        return {}
    region = r["id"]
    readings = [{"path": x["path"], "summary": x["summary"]}
                for x in conn.execute(
                    "SELECT co.path, rd.summary FROM readings rd"
                    " JOIN code_objects co ON co.id=rd.object_id"
                    " WHERE co.path LIKE ? || '%' ORDER BY co.path", (region,))]
    chains = [{"id": c["id"], "invariants": c["hop_invariants"]}
              for c in conn.execute(
                  "SELECT id, hop_invariants FROM chains WHERE status='active'"
                  " AND nodes LIKE '%' || ? || '%'", (region,))]
    return {"region": region, "readings": readings[:20], "chains": chains[:5],
            "note": "Explore ONLY this region (disjoint lease). Write function "
                    "notes (invariant + 3 ranked candidates) BEFORE proposing "
                    "a probe."}


@context_provider("hunt_method")
def _hunt_method(env, task, spec):
    """The detection method dispatched to this explorer (recorded on the task
    at region-lease time) — how to GENERATE the candidate (invariant-probe,
    metamorphic-flip, …). Read, not re-picked, so the prompt and the merge
    credit agree. The verification oracle is separate and never rotates."""
    mr = env.conn.execute("SELECT json_extract(payload,'$.method') m FROM tasks"
                          " WHERE id=?", (task["id"],)).fetchone()
    m = mr["m"] if mr else None
    if not m:
        return {}
    row = env.conn.execute("SELECT description FROM methods WHERE id=?",
                           (m,)).fetchone()
    return {"method": m, "how": row["description"] if row else "",
            "note": "Generate this round's candidate using the '%s' method." % m}


@context_provider("hunt_arsenal")
def _hunt_arsenal(env, task, spec):
    """What the Oracle-Scout reasons over: the ACTIVE bench (still rotating),
    the EXHAUSTED arms (don't re-propose these), and a sample of CONFIRMED
    findings (the mechanisms to generalize into new methods)."""
    conn = env.conn
    active = [{"id": r["id"], "how": r["description"]} for r in conn.execute(
        "SELECT id, description FROM methods WHERE status='active' ORDER BY id")]
    exhausted = [r["id"] for r in conn.execute(
        "SELECT id FROM methods WHERE status='exhausted' ORDER BY id")]
    findings = [{"title": r["title"], "pattern": None} for r in conn.execute(
        "SELECT title FROM findings WHERE source='bughunt'"
        " ORDER BY id DESC LIMIT 20")]
    return {"active": active, "exhausted": exhausted, "confirmed_findings": findings,
            "note": "Invent methods NOT in active or exhausted. Generalize a "
                    "confirmed finding's mechanism, or target a pattern class "
                    "no active method provokes."}


@block("hunt.merge_scout", "state", {"proposed", "saturated"},
       required_params={"repo"})
def hunt_merge_scout(ctx, task, prev):
    """Apply the Oracle-Scout's proposals. PROPOSED with >=1 genuinely new
    method reopens the hunt: insert the new arms 'active' (trials=0 -> the
    bandit pulls them FIRST, which IS their trial; TRIAL_BUDGET zero-yield
    pulls later retires them), clear region cooldowns so the fresh tactics
    get a full surface, and emit explore_requested. NO_NEW_METHOD (or a
    proposal of only-already-known ids, or the scout-round cap) ends the
    campaign. INSERT OR IGNORE never revives an exhausted id."""
    conn = ctx["_conn"]
    res = prev or {}
    rnd = _round(conn)
    scouted = _bump(conn, "hunt.scout_rounds")
    added = 0
    if res.get("verdict") == "PROPOSED" and scouted <= MAX_SCOUT_ROUNDS:
        have = {r["id"] for r in conn.execute("SELECT id FROM methods")}
        for m in res.get("methods") or []:
            mid = (m.get("id") or "").strip()
            if not mid or mid in have:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO methods(id, description, status)"
                " VALUES (?,?, 'active')", (mid, (m.get("description") or "")[:400]))
            have.add(mid)
            added += 1
    if not added:
        return "saturated", {"scout_round": scouted, "added": 0}
    # reopen: a new tactic makes cooled regions worth revisiting
    conn.execute("UPDATE regions SET dry_streak=0, cooldown_until_round=NULL")
    return "proposed", {"scout_round": scouted, "added": added,
                        "_staged": [{"op": "emit_event",
                                     "name": "hunt.explore_requested",
                                     "payload": {"round": rnd}}]}


def _bump(conn, scope):
    """Increment and return a watermark counter (a logical round/limit clock)."""
    n = 1
    r = conn.execute("SELECT cursor FROM watermarks WHERE scope=?", (scope,)).fetchone()
    if r:
        n = int(r["cursor"]) + 1
    conn.execute("INSERT INTO watermarks(scope, cursor) VALUES (?,?)"
                 " ON CONFLICT(scope) DO UPDATE SET cursor=?", (scope, str(n), str(n)))
    return n


@block("hunt.verify_candidate", "local",
       {"confirmed", "refuted", "no_candidate", "error", "timeout"},
       required_params={"repo", "clang"})
def hunt_verify_candidate(ctx, task, prev):
    """The verification oracle (invariant, non-negotiable): a candidate
    becomes a finding ONLY if its repro reproduces against the base compiler,
    classified by exit code + expected. The LLM proposed the probe + the
    invariant; this decides truth. Crash (exit>128 / signal) is always a bug.
    """
    res = (prev or {})
    passthrough = {"region": res.get("region"), "method": res.get("method"),
                   "round": res.get("round"), "note": res.get("note")}
    verdict = res.get("verdict")
    finding = res.get("finding") or {}
    if verdict != "CONFIRMED_NEW" or not finding.get("probe"):
        return "no_candidate", dict(passthrough, verified={"confirmed": False})

    clang = template(ctx["clang"], {})
    sd = Path(ctx["_step_dir"])
    probe = sd / "cand.cbs"
    probe.write_text(finding["probe"])
    inc = ctx.get("include")
    cmd = [clang, "-fsyntax-only", "-Wno-nullability-completeness"]
    if inc:
        cmd += ["-I", template(inc, {})]
    cmd.append(str(probe))
    code, out, err = run_cmd(cmd, ctx["_timeout_s"], sd / "run",
                             tools=ctx.get("_tools"))
    errored = code != 0
    crashed = code is not None and code > 128
    stderr = Path(err).read_text(errors="replace")
    expect_error = bool(finding.get("expect_error"))
    # the compiler is BUGGY (a real finding) when it violates the invariant:
    #  - should reject unsafe code but accepted it (missed diagnostic), or
    #  - should accept safe code but rejected it (false positive), or
    #  - crashed on any input.
    if crashed:
        confirmed, why = True, "compiler crashed (exit %s)" % code
    elif expect_error and not errored:
        confirmed, why = True, "unsafe code accepted (missed diagnostic)"
    elif (not expect_error) and errored:
        confirmed, why = True, "safe code rejected (false positive)"
    else:
        confirmed, why = False, "compiler behaved as the invariant expects"
    want = finding.get("expect_contains")
    if confirmed and want and expect_error and errored and want not in stderr:
        # errored but not for the claimed reason -> don't trust it
        confirmed, why = False, "errored but not with the expected diagnostic"
    verified = {"confirmed": confirmed, "why": why, "exit_code": code,
                "key": finding.get("key"), "title": finding.get("title"),
                "pattern": finding.get("pattern"),
                "severity": finding.get("severity", "medium"),
                "grep_rule": finding.get("grep_rule"),
                "evidence": {"why": why, "exit_code": code,
                             "stderr_path": err, "probe": finding["probe"][:500]}}
    return ("confirmed" if confirmed else "refuted"), dict(passthrough,
                                                           verified=verified)


@block("hunt.merge_exploit", "state", {"ok", "timeout"},
       required_params={"repo", "clang"})
def hunt_merge_exploit(ctx, task, prev):
    """Verify each DISTINCT variant against base clang and file the confirmed
    ones (folded under the parent pattern). FOLDED variants just count as
    breadth; SHAPE_REJECTED/INCONCLUSIVE are dropped. Bounded to <=8."""
    res = prev or {}
    variants = (res.get("variants") or [])[:8]
    payload = task.get("payload") or {}
    pattern = payload.get("pattern")
    repo = template(ctx["repo"], {})
    clang = template(ctx["clang"], {})
    inc = template(ctx["include"], {}) if ctx.get("include") else None
    sd = Path(ctx["_step_dir"])
    staged, filed, folded = [], 0, 0
    for i, v in enumerate(variants):
        if v.get("verdict") == "FOLDED":
            folded += 1
            continue
        if v.get("verdict") != "DISTINCT" or not v.get("probe"):
            continue
        p = sd / ("v%d.cbs" % i)
        p.write_text(v["probe"])
        cmd = [clang, "-fsyntax-only", "-Wno-nullability-completeness"]
        if inc:
            cmd += ["-I", inc]
        cmd.append(str(p))
        code, _o, _e = run_cmd(cmd, ctx["_timeout_s"], sd / ("run%d" % i),
                               tools=ctx.get("_tools"))
        errored = code != 0
        crashed = code is not None and code > 128
        want_err = bool(v.get("expect_error"))
        if crashed or (want_err and not errored) or ((not want_err) and errored):
            filed += 1
            staged.append({"op": "upsert_finding",
                           "key": v.get("key") or "%s-variant-%d" % (pattern, i),
                           "title": (v.get("title") or "variant of %s" % pattern)[:200],
                           "source": "bughunt", "repo": repo,
                           "severity": "medium", "pattern": pattern})
    return "ok", {"_staged": staged, "filed": filed, "folded": folded,
                  "variants": len(variants)}
