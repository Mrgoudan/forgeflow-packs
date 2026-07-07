"""The Conductor — HUNT.md's dispatch, as deterministic db functions.

Per HUNT.md the Conductor is NOT an agent: it is the daemon + dispatch
rules, where every decision is a pure function of db state at claim time,
serialized through the single-writer queue so replaying the event log
reproduces the run. These blocks are those rules:

- hunt.seed          seed regions + methods bench from pack config (idempotent)
- hunt.pick_region   lease the next region deterministically (cap-enforced)
- hunt.pick_method   UCB bandit over the methods bench (deterministic, ties by id)
- hunt.merge_explore apply an explorer's VERIFIED result: file finding+pattern,
                     update dry_streak/cooldown, and enqueue the replacement
                     explorer (auto-swap) + an exploiter on a confirm.

Rounds are a logical clock in watermarks('hunt.round'); merge ticks it.
Explore vs exploit balance is emergent (every confirm spawns one exploiter;
every return spawns one replacement explorer) — not a knob.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from forgeflow.blocks import block
from forgeflow.util import template

COOLDOWN_C = 3          # rounds a region cools after dry_streak hits the limit
DRY_LIMIT = 3           # consecutive dry explores -> cooldown


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
    """Seed the regions surface and the methods bench from pack config.
    Idempotent — INSERT OR IGNORE, so re-running never disturbs live state."""
    conn = ctx["_conn"]
    repo = template(ctx["repo"], {})
    for rid in ctx.get("regions") or []:
        conn.execute("INSERT OR IGNORE INTO regions(id, repo) VALUES (?,?)",
                     (rid, repo))
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
    return "leased", {"region": row["id"], "round": rnd}


def _pick_method(conn):
    """Deterministic UCB bandit over the methods bench: index =
    verified_yield/trials + sqrt(2*ln(round+1)/trials); untried methods get
    +inf so they're tried first; argmax, ties by id. Pure function of db
    state — pick (for the explorer's context) and credit (in merge) call this
    same helper, so they agree as long as trials haven't moved between them."""
    rnd = _round(conn) + 1
    best, best_ix = None, -1.0
    for m in conn.execute("SELECT id, trials, verified_yield FROM methods"
                          " WHERE status='active' ORDER BY id"):
        t = m["trials"]
        ix = float("inf") if t == 0 else (
            m["verified_yield"] / t + math.sqrt(2 * math.log(rnd) / t))
        if ix > best_ix:
            best, best_ix = m["id"], ix
    return best


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
    # through) and method from the SAME deterministic bandit the explorer saw
    # (trials haven't moved yet, so it re-picks the same one), then credit.
    lr = conn.execute("SELECT id FROM regions WHERE leased_by_task=?",
                      (task["id"],)).fetchone()
    region = lr["id"] if lr else None
    method = _pick_method(conn)
    verified = (prev or {}).get("verified") or {}
    confirmed = bool(verified.get("confirmed"))
    rnd = _tick_round(conn)
    staged, emits = [], []

    if method:
        conn.execute("UPDATE methods SET trials=trials+1 WHERE id=?", (method,))

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

    # auto-swap: hand off to the next region unless the campaign is saturated
    nxt = conn.execute(
        "SELECT id FROM regions WHERE leased_by_task IS NULL"
        " AND (cooldown_until_round IS NULL OR cooldown_until_round <= ?)"
        " LIMIT 1", (rnd,)).fetchone()
    if nxt is not None:
        emits.append({"op": "emit_event", "name": "hunt.explore_requested",
                      "payload": {"round": rnd}})
        result = {"round": rnd, "region": region, "_staged": staged + emits}
        return outcome, result
    # campaign saturated: nothing to hand off
    return "done", {"round": rnd, "region": region, "_staged": staged + emits,
                    "campaign": "saturated"}


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
    """The detection method the bandit selected for this explore round — how
    to GENERATE the candidate (invariant-probe, metamorphic-flip, …). The
    verification oracle is separate and never rotates."""
    m = _pick_method(env.conn)
    if not m:
        return {}
    row = env.conn.execute("SELECT description FROM methods WHERE id=?",
                           (m,)).fetchone()
    return {"method": m, "how": row["description"] if row else "",
            "note": "Generate this round's candidate using the '%s' method." % m}


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
                   "round": res.get("round")}
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
