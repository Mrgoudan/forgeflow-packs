#!/usr/bin/env python3
"""forgeflow BSC control room — one self-contained (stdlib-only) local server
that shows the live system AND drives it.

It IS the daemon: a gated claim->execute loop runs in the main thread and
respects the control flags the web UI writes to the `dash_control` table
(global pause + per-capability enable). So the buttons really start / pause /
gate work, and the page really reflects the db — stats, the live queue, and
each workflow drawn as its block graph with per-block throughput.

Launch with the same env as run-bsc.sh (so agent tasks can reach GLM):

    ./run-bsc.sh dash                       # if wired
    FF_ROOT=../../run PACK_DIR=. python3 dashboard.py --port 8787
"""
from __future__ import annotations

import argparse
import glob
import inspect
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ENGINE = os.environ.get("ENGINE", str(Path.home() / "bsd/forgeflow"))
sys.path.insert(0, ENGINE)

from forgeflow import config, db, queue                 # noqa: E402
from forgeflow.engine import Engine                     # noqa: E402
from forgeflow.util import tx                           # noqa: E402

# ------------------------------------------------------------ capabilities
CAPS = ("hunt", "review", "fix")


def cap_of(kind):
    if kind.startswith("hunt"):
        return "hunt"
    if kind in ("review", "pr_intake", "pr_fetch", "pr_report"):
        return "review"
    if kind == "fix_finding":
        return "fix"
    return None


# ------------------------------------------------------------ control flags
_DEFAULTS = {"paused": "0", "cap_hunt": "1", "cap_review": "1", "cap_fix": "1",
             "hunt_continuous": "0", "hunt_base": ""}
POLL_S, FIX_S, HUNT_S = 300, 30, 60          # auto-trigger intervals


def init_control(conn):
    with tx(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS dash_control("
                     "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for k, v in _DEFAULTS.items():
            conn.execute("INSERT OR IGNORE INTO dash_control(key, value) VALUES (?,?)",
                         (k, v))


def flag(conn, key):
    r = conn.execute("SELECT value FROM dash_control WHERE key=?", (key,)).fetchone()
    return r["value"] if r else _DEFAULTS.get(key, "0")


def set_flag(conn, key, value):
    with tx(conn):
        conn.execute("INSERT INTO dash_control(key, value) VALUES (?,?)"
                     " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))


# ------------------------------------------------------------ gated daemon
class Daemon:
    def __init__(self, eng):
        self.eng = eng
        self.conn = eng.conn
        self.stop = threading.Event()
        self.last_error = None
        self.executed = 0
        self._t = {"poll": 0.0, "fix": 0.0, "hunt": 0.0}

    def tick(self):
        """The daemon's own clock = the auto-triggers. Review polls for new
        PRs; fix fires on any new triaged finding; hunt re-opens a round when
        it's gone idle AND continuous is on. All gated by the capability flags
        (so a disabled/`paused` capability never auto-fires)."""
        now, c, subs = time.monotonic(), self.conn, self.eng.subscriptions
        nonce = int(time.time())          # keeps repeat triggers from deduping
        emits = []
        if flag(c, "cap_review") == "1" and now - self._t["poll"] > POLL_S:
            self._t["poll"] = now
            emits.append(("forge.poll_requested", {"poll": nonce}))
        base = flag(c, "hunt_base") or "HEAD"
        if flag(c, "cap_fix") == "1" and now - self._t["fix"] > FIX_S:
            self._t["fix"] = now
            for r in c.execute("SELECT key FROM findings WHERE state='triaged'"
                               " AND branch IS NULL"):
                emits.append(("fix.requested", {"finding": r["key"], "base": base}))
        if (flag(c, "cap_hunt") == "1" and flag(c, "hunt_continuous") == "1"
                and now - self._t["hunt"] > HUNT_S):
            n = c.execute("SELECT count(*) FROM tasks WHERE kind LIKE 'hunt%'"
                          " AND state IN ('pending','running','retry_wait')").fetchone()[0]
            if n == 0:
                self._t["hunt"] = now
                emits.append(("hunt.round_requested", {"base": base, "kick": nonce}))
        if emits:
            with tx(c):
                for name, payload in emits:
                    db.emit_event(c, name, payload, subs)

    def loop(self):
        idle = 1.0
        while not self.stop.is_set():
            if flag(self.conn, "paused") == "1":
                time.sleep(idle)
                continue
            try:
                self.tick()
            except Exception as e:
                self.last_error = "tick: %s" % e
            try:
                task = queue.claim(self.conn)
            except Exception as e:                       # keep the daemon alive
                self.last_error = "claim: %s" % e
                time.sleep(idle)
                continue
            if task is None:
                time.sleep(idle)
                continue
            cap = cap_of(task["kind"])
            if cap and flag(self.conn, "cap_" + cap) == "0":
                with tx(self.conn):
                    self.conn.execute(
                        "UPDATE tasks SET state='parked', park_reason=? WHERE id=?",
                        ("dash:%s disabled" % cap, task["id"]))
                continue
            try:
                self.eng.execute_one(task)
                self.executed += 1
            except Exception as e:
                self.last_error = "task %s: %s" % (task["id"], e)


# ------------------------------------------------------------ reads (stats)
def _counts(conn, sql, args=()):
    return {r[0]: r[1] for r in conn.execute(sql, args)}


def snapshot(conn):
    tasks = _counts(conn, "SELECT state, count(*) FROM tasks GROUP BY state")
    findings = _counts(conn, "SELECT state, count(*) FROM findings GROUP BY state")
    methods = _counts(conn, "SELECT status, count(*) FROM methods GROUP BY status")
    regions = conn.execute(
        "SELECT count(*) t,"
        " sum(leased_by_task IS NOT NULL) leased,"
        " sum(cooldown_until_round IS NOT NULL) cooling FROM regions").fetchone()
    rnd = conn.execute("SELECT cursor FROM watermarks WHERE scope='hunt.round'").fetchone()
    top_methods = [dict(id=r["id"], trials=r["trials"], yield_=r["verified_yield"],
                        status=r["status"])
                   for r in conn.execute(
                       "SELECT id, status, trials, verified_yield FROM methods"
                       " ORDER BY verified_yield DESC, trials DESC, status, id LIMIT 8")]
    prs = conn.execute("SELECT count(*) FROM findings WHERE pr_number IS NOT NULL").fetchone()[0]
    active = [dict(id=r["id"], kind=r["kind"], state=r["state"], attempts=r["attempts"],
                   step=r["step"], age=r["age"])
              for r in conn.execute(
                  "SELECT t.id, t.kind, t.state, t.attempts,"
                  " (SELECT step FROM task_steps s WHERE s.task_id=t.id"
                  "  ORDER BY s.at DESC LIMIT 1) step,"
                  " CAST((julianday('now')-julianday(t.updated_at))*86400 AS INT) age"
                  " FROM tasks t WHERE t.state IN"
                  " ('pending','running','retry_wait','parked')"
                  " ORDER BY t.updated_at DESC LIMIT 40")]
    events = [dict(name=r["name"], at=r["at"]) for r in conn.execute(
        "SELECT name, at FROM events ORDER BY id DESC LIMIT 12")]
    return {
        "tasks": tasks, "findings": findings, "methods": methods,
        "regions": {"total": regions["t"] or 0, "leased": regions["leased"] or 0,
                    "cooling": regions["cooling"] or 0},
        "round": int(rnd["cursor"]) if rnd else 0, "prs": prs,
        "top_methods": top_methods, "queue": active, "events": events,
        "control": {k: flag(conn, k) for k in _DEFAULTS},
    }


def _throughput(conn):
    """per (workflow-kind, step): how many times the block ran + avg ms."""
    out = {}
    for r in conn.execute(
            "SELECT t.kind, s.step, count(*) n, CAST(avg(s.wall_ms) AS INT) ms"
            " FROM task_steps s JOIN tasks t ON t.id=s.task_id"
            " GROUP BY t.kind, s.step"):
        out.setdefault(r["kind"], {})[r["step"]] = {"n": r["n"], "ms": r["ms"]}
    return out


def _parse_workflows(workflow_dirs):
    """{name: {consumes, emits, steps:[{name, block, llm, schema, outcomes,
    context, params, timeout_s}]}} from the YAML (the graph source of truth)."""
    wfs = {}
    for d in workflow_dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            try:
                wf = yaml.safe_load(Path(path).read_text())
            except Exception:
                continue
            name = wf.get("workflow")
            if not name or name in wfs:
                continue
            steps = []
            for st in wf.get("steps", []):
                steps.append({
                    "name": st.get("name"),
                    "block": st.get("block") or (
                        "agent.run · %s" % st.get("llm") if st.get("llm") else "?"),
                    "llm": st.get("llm"), "schema": st.get("schema"),
                    "outcomes": st.get("outcomes", {}) or {},
                    "context": st.get("context", []) or [],
                    "params": st.get("params", {}) or {},
                    "timeout_s": st.get("timeout_s")})
            wfs[name] = {"consumes": wf.get("consumes", []),
                         "emits": wf.get("emits", []), "steps": steps}
    return wfs


def _running_positions(conn, wfs):
    """{kind: {step: count}} — the exact block each running task is ON, routed
    from its last completed (step, outcome) through the graph (no task_steps
    yet for the in-flight step, so we route the previous one's outcome)."""
    pos = {}
    for r in conn.execute("SELECT id, kind FROM tasks WHERE state='running'"):
        wf = wfs.get(r["kind"])
        if not wf or not wf["steps"]:
            continue
        last = conn.execute("SELECT step, outcome FROM task_steps WHERE task_id=?"
                            " ORDER BY at DESC LIMIT 1", (r["id"],)).fetchone()
        if last is None:
            step = wf["steps"][0]["name"]
        else:
            step = None
            for s in wf["steps"]:
                if s["name"] == last["step"]:
                    step = s["outcomes"].get(last["outcome"])
                    break
        if step:
            pos.setdefault(r["kind"], {})[step] = \
                pos.setdefault(r["kind"], {}).get(step, 0) + 1
    return pos


def workflow_graphs(workflow_dirs, conn):
    """The block graphs annotated with live throughput + who's running now."""
    tp = _throughput(conn)
    wfs = _parse_workflows(workflow_dirs)
    pos = _running_positions(conn, wfs)
    graphs = []
    for name, wf in wfs.items():
        steps = []
        for st in wf["steps"]:
            m = tp.get(name, {}).get(st["name"], {})
            steps.append({"name": st["name"], "block": st["block"],
                          "outcomes": st["outcomes"], "ran": m.get("n", 0),
                          "ms": m.get("ms"),
                          "running": pos.get(name, {}).get(st["name"], 0)})
        graphs.append({"name": name, "cap": cap_of(name),
                       "consumes": wf["consumes"], "emits": wf["emits"],
                       "steps": steps})
    order = {"review": 0, "hunt": 1, "fix": 2, None: 3}
    graphs.sort(key=lambda g: (order.get(g["cap"], 3), g["name"]))
    return graphs


def _read_pack_text(val):
    """A pack prompt/schema value may be a file path or inline text/dict."""
    if isinstance(val, dict):
        return yaml.safe_dump(val, sort_keys=False)
    s = str(val)
    try:
        if os.path.isfile(s):
            return Path(s).read_text(errors="replace")
    except OSError:
        pass
    return s


def _block_doc(block_id):
    """What a block DOES — its function docstring, falling back to the module
    docstring so a block that skipped its own docstring still says something."""
    try:
        from forgeflow.blocks import get
        fn = get(block_id).fn
        return inspect.getdoc(fn) or (inspect.getdoc(inspect.getmodule(fn)) or "")
    except Exception:
        return ""


def block_detail(conn, pack, workflow_dirs, wf_name, step_name):
    """Everything behind a block card: WHAT IT DOES (docstring), its wiring,
    its PROMPT (for agent steps), its output schema, and its recent runs."""
    wf = _parse_workflows(workflow_dirs).get(wf_name)
    if not wf:
        return {"error": "no such workflow"}
    st = next((s for s in wf["steps"] if s["name"] == step_name), None)
    if not st:
        return {"error": "no such step"}
    block_id = "agent.run" if st["llm"] else st["block"]
    out = {"workflow": wf_name, "step": step_name, "block": st["block"],
           "block_id": block_id, "desc": _block_doc(block_id),
           "llm": st["llm"], "schema": st["schema"], "context": st["context"],
           "outcomes": st["outcomes"], "params": st["params"],
           "timeout_s": st["timeout_s"]}
    if st["llm"]:
        pf = (pack.prompts or {}).get(st["llm"])
        out["prompt"] = _read_pack_text(pf) if pf else "(no prompt registered)"
    if st["schema"]:
        sc = (pack.schemas or {}).get(st["schema"])
        out["schema_def"] = _read_pack_text(sc) if sc is not None else None
    out["runs"] = [dict(outcome=r["outcome"], ms=r["wall_ms"], at=r["at"],
                        result=(r["result"] or "")[:600])
                   for r in conn.execute(
                       "SELECT s.outcome, s.wall_ms, s.at, s.result"
                       " FROM task_steps s JOIN tasks t ON t.id=s.task_id"
                       " WHERE t.kind=? AND s.step=? ORDER BY s.at DESC LIMIT 8",
                       (wf_name, step_name))]
    return out


# ------------------------------------------------------------ actions
def do_action(conn, subs, action, params):
    if action == "pause":
        set_flag(conn, "paused", 1)
    elif action == "resume":
        set_flag(conn, "paused", 0)
    elif action in ("enable", "disable"):
        cap = params.get("cap")
        if cap in CAPS:
            set_flag(conn, "cap_" + cap, 1 if action == "enable" else 0)
            if action == "enable":                        # release what we parked
                with tx(conn):
                    conn.execute(
                        "UPDATE tasks SET state='pending', park_reason=NULL"
                        " WHERE state='parked' AND park_reason=?",
                        ("dash:%s disabled" % cap,))
    elif action == "run_hunt":
        base = params.get("base") or "HEAD"
        set_flag(conn, "hunt_base", base)                  # continuous reuses it
        db.emit_event(conn, "hunt.round_requested",
                      {"base": base, "kick": int(time.time())}, subs)
    elif action == "continuous":                           # hunt: keep re-opening
        set_flag(conn, "hunt_continuous", "1" if params.get("on") else "0")
        if params.get("base"):
            set_flag(conn, "hunt_base", params["base"])
    elif action == "run_explore":                          # one explore turn
        db.emit_event(conn, "hunt.explore_requested", {"round": int(time.time())}, subs)
    elif action == "run_scout":                            # the method finder, alone
        db.emit_event(conn, "hunt.scout_requested", {"round": int(time.time())}, subs)
    elif action == "run_exploit":                          # a saved pattern, alone
        pat = (params.get("pattern") or "").strip()
        if not pat:
            return {"error": "pattern id required (e.g. C1)"}
        db.emit_event(conn, "hunt.pattern_confirmed", {"pattern": pat}, subs)
        return {"ok": True, "pattern": pat}
    elif action == "run_review":
        db.emit_event(conn, "forge.poll_requested", {"poll": int(time.time())}, subs)
    elif action == "run_fix":
        base = params.get("base") or "HEAD"
        n = 0
        for r in conn.execute("SELECT key FROM findings WHERE state='triaged'"):
            db.emit_event(conn, "fix.requested", {"finding": r["key"], "base": base}, subs)
            n += 1
        return {"queued": n}
    else:
        return {"error": "unknown action %r" % action}
    return {"ok": True}


# ------------------------------------------------------------ http
class Handler(BaseHTTPRequestHandler):
    db_path = None
    subs = None
    workflow_dirs = ()
    daemon = None
    pack = None

    def log_message(self, *a):                            # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        conn = db.connect(self.db_path)
        try:
            if self.path.startswith("/api/state"):
                snap = snapshot(conn)
                snap["daemon"] = {"alive": self.daemon is not None
                                  and not self.daemon.stop.is_set(),
                                  "executed": self.daemon.executed if self.daemon else 0,
                                  "error": self.daemon.last_error if self.daemon else None}
                return self._send(200, json.dumps(snap))
            if self.path.startswith("/api/workflows"):
                return self._send(200, json.dumps(
                    workflow_graphs(self.workflow_dirs, conn)))
            if self.path.startswith("/api/block"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, json.dumps(block_detail(
                    conn, self.pack, self.workflow_dirs,
                    (q.get("wf") or [""])[0], (q.get("step") or [""])[0])))
        finally:
            conn.close()
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if not self.path.startswith("/api/control"):
            return self._send(404, json.dumps({"error": "not found"}))
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"error": "bad json"}))
        conn = db.connect(self.db_path)
        try:
            res = do_action(conn, self.subs, body.get("action"), body.get("params") or {})
        finally:
            conn.close()
        self._send(200, json.dumps(res))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("FF_ROOT", "../../run"))
    ap.add_argument("--pack", default=os.environ.get("PACK_DIR",
                                                     os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASH_PORT", "8787")))
    args = ap.parse_args()

    eng = Engine(args.root, pack=config.load_pack(args.pack))
    init_control(eng.conn)
    daemon = Daemon(eng)

    Handler.db_path = str(Path(args.root) / "state" / "forgeflow.db")
    Handler.subs = eng.subscriptions
    Handler.workflow_dirs = list(eng.pack.workflow_dirs)
    Handler.daemon = daemon
    Handler.pack = eng.pack

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    print("forgeflow control room → http://127.0.0.1:%d  (db: %s)"
          % (args.port, Handler.db_path))
    try:
        daemon.loop()                                     # runs in the main thread
    except KeyboardInterrupt:
        daemon.stop.set()
        httpd.shutdown()
        print("\nstopped.")


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>forgeflow · BSC control room</title>
<style>
:root{--bg:#0a0d13;--surface:#141924;--surface2:#1b2130;--line:#262d3d;
--fg:#e7eaf0;--muted:#8b95a8;--accent:#5b9dff;--accent2:#2f6fed;
--ok:#43c463;--warn:#e0a63a;--bad:#f0554d;--r:12px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
pre,code,.bl,.block .sn,.pillt,.chip{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
a{color:var(--accent)}
header{display:flex;align-items:center;gap:16px;padding:12px 22px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:rgba(10,13,19,.82);backdrop-filter:blur(10px);z-index:30}
.brand{font-size:16px;font-weight:700;text-decoration:none;color:var(--fg);letter-spacing:.01em;white-space:nowrap}
.brand .dim{color:var(--muted);font-weight:400}
nav{display:flex;gap:3px}
.navtab{padding:7px 15px;border-radius:9px;text-decoration:none;color:var(--muted);font-size:13px;font-weight:500;transition:.12s}
.navtab:hover{color:var(--fg);background:var(--surface2)}
.navtab.active{color:#fff;background:var(--accent2)}
.spacer{flex:1}
.pill{padding:3px 11px;border-radius:20px;border:1px solid var(--line);font-size:12px;font-weight:500;color:var(--muted);white-space:nowrap}
.pill.on{color:var(--ok);border-color:rgba(67,196,99,.35);background:rgba(67,196,99,.08)}
.pill.off{color:var(--warn);border-color:rgba(224,166,58,.35);background:rgba(224,166,58,.08)}
button{font:inherit;font-size:13px;font-weight:500;background:var(--surface2);color:var(--fg);
border:1px solid var(--line);border-radius:9px;padding:6px 13px;cursor:pointer;transition:.12s}
button:hover{border-color:var(--accent);background:#232b3a}
button.run{color:var(--ok);border-color:rgba(67,196,99,.4)}.pausebtn{font-weight:600}
input{font:inherit;font-size:13px;background:#0d1119;color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:6px 9px;width:150px}input:focus{outline:none;border-color:var(--accent)}
main{max-width:1180px;margin:0 auto;padding:22px}
.h{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:24px 0 12px}
.h:first-child{margin-top:4px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 10px;font-weight:600}
.row{display:flex;justify-content:space-between;gap:8px;padding:2px 0}.row .k{color:var(--muted)}
.tag{font-size:11px;color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:14px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:17px 18px}
.kpi .num{font-size:32px;font-weight:700;line-height:1;letter-spacing:-.02em}
.kpi.accent .num{color:var(--accent)}
.kpi .lbl{font-size:12px;color:var(--muted);margin-top:7px;text-transform:uppercase;letter-spacing:.04em;font-weight:500}
.kpi .sub{font-size:12px;color:var(--muted);margin-top:9px}
.capgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.cap{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px}
.cap .top{display:flex;align-items:center;gap:9px;margin-bottom:6px;flex-wrap:wrap}
.cap .name{font-weight:600;font-size:15px;text-transform:capitalize}
.cap .ctl{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.feed .frow{display:flex;justify-content:space-between;align-items:center;padding:8px 6px;font-size:13px;border-bottom:1px solid rgba(38,45,61,.55)}
.feed .frow:last-child{border:none}
table{width:100%;border-collapse:collapse}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.03em}
.st-running{color:var(--accent)}.st-parked,.st-retry_wait{color:var(--warn)}.st-failed{color:var(--bad)}.st-pending{color:var(--fg)}
.capsec{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;margin-bottom:16px;overflow-x:auto}
.caph{font-size:15px;font-weight:600;color:var(--fg);margin:0 0 4px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.caph a{font-size:12px}.sub{color:var(--muted);font-size:12px;margin-bottom:10px}
.pipe{display:flex;flex-direction:column;align-items:center;padding-top:6px}
.hop{display:flex;flex-direction:column;align-items:center}
.hop .bar{width:1px;height:13px;background:var(--accent2)}
.hop .ev{background:rgba(47,111,237,.14);border:1px solid rgba(47,111,237,.5);border-radius:20px;padding:2px 12px;color:#9dc1ff;font-size:11px;white-space:nowrap}
.hop.trig .ev{background:var(--surface2);border-color:var(--line);color:var(--muted)}
.hop.loop .ev{background:rgba(224,166,58,.12);border-color:rgba(224,166,58,.5);color:var(--warn)}
.parallelrow{display:flex;align-items:flex-start}
.spawncol{display:flex;flex-direction:column;gap:14px;padding-top:14px}
.spawn{display:flex;align-items:flex-start}
.harrow{display:flex;align-items:center;color:#9dc1ff;font-size:10px;white-space:nowrap;padding-top:18px}
.harrow::before{content:'';width:16px;height:1px;background:var(--accent2);margin-right:4px}
.harrow::after{content:'▶';color:var(--accent2);margin-left:2px}
.seg{display:flex;flex-direction:column;align-items:center}
.seglabel{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:20px;padding:2px 11px;margin-bottom:6px;text-decoration:none;display:inline-block}
.seglabel:hover{border-color:var(--accent)}.seglabel b{color:var(--fg)}
.crossnote{font-size:11px;color:var(--muted);margin:5px 0 2px}.crossnote b{color:#9dc1ff}
.tree{overflow-x:auto;padding:6px 2px}
.treecol{display:flex;flex-direction:column;align-items:center}
.tree .block{width:186px}
.down{height:24px;display:flex;align-items:center;justify-content:center;position:relative}
.down::before{content:'';position:absolute;top:0;bottom:0;width:1px;background:var(--line)}
.down span{background:var(--surface);padding:0 6px;position:relative;z-index:1;color:var(--muted);font-size:10px}
.fork{width:1px;height:12px;background:var(--line)}
.branches{display:flex;gap:18px;align-items:flex-start;padding-top:12px;position:relative}
.branches::before{content:'';position:absolute;top:0;left:12%;right:12%;height:1px;background:var(--line)}
.branch{display:flex;flex-direction:column;align-items:center;position:relative}
.branch::before{content:'';position:absolute;top:-12px;left:50%;width:1px;height:12px;background:var(--line)}
.branch>.edge{color:#9dc1ff;font-size:10px;margin:2px 0 5px;white-space:nowrap}
.termpills{display:flex;flex-wrap:wrap;gap:4px;justify-content:center;margin:6px 0 2px;max-width:190px}
.pillt{font-size:10px;padding:2px 9px;border-radius:20px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.pillt.ref{border-style:dashed;cursor:pointer;color:var(--accent)}
.block{background:#0d1119;border:1px solid var(--line);border-radius:9px;padding:9px 11px;cursor:pointer;transition:.1s;position:relative;overflow:hidden}
.block:hover{transform:translateY(-2px);border-color:var(--accent)}
.block .sn{font-weight:600;font-size:13px}
.block .bl{color:var(--accent);font-size:11px;word-break:break-all;margin-top:1px}
.block .ran{color:var(--muted);font-size:11px;margin-top:6px}
.block.running{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok),0 0 16px rgba(67,196,99,.25)}
.block.running::after{content:'';position:absolute;top:8px;right:8px;width:8px;height:8px;border-radius:50%;background:var(--ok);animation:pulse 1.1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.badge{display:inline-block;min-width:18px;text-align:center;background:var(--surface2);border-radius:20px;padding:1px 7px;color:var(--ok);font-size:11px}.badge.run{background:rgba(67,196,99,.18)}
.wflink{display:flex;justify-content:space-between;align-items:center;padding:10px 13px;margin:4px 0;background:var(--surface);border:1px solid var(--line);border-radius:9px;text-decoration:none;color:var(--fg)}
.wflink:hover{border-color:var(--accent);background:var(--surface2)}
.caplink{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin:14px 0 6px;text-decoration:none;font-weight:600}
.caplink:hover{color:var(--accent)}
.back{display:inline-block;color:var(--muted);text-decoration:none;font-size:13px;margin-bottom:14px}
.back:hover{color:var(--accent)}
.drawer{position:fixed;top:0;right:0;height:100%;width:min(580px,96vw);background:var(--surface);border-left:1px solid var(--line);transform:translateX(100%);transition:.2s;z-index:50;overflow-y:auto;padding:20px 22px}
.drawer.open{transform:none}.drawer h3{margin:0 0 3px;font-size:18px}
.drawer .x{position:absolute;top:14px;right:16px;background:none;border:none;color:var(--muted);font-size:22px;cursor:pointer}
.drawer .sec{margin-top:16px}.drawer .sec h4{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 6px;font-weight:600}
.whatdoes{white-space:pre-wrap;color:var(--fg);font-size:13px;line-height:1.6}
pre{background:#0d1119;border:1px solid var(--line);border-radius:9px;padding:11px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-size:12px;margin:0}
.chip{display:inline-block;background:var(--surface2);border:1px solid var(--line);border-radius:20px;padding:2px 10px;font-size:11px;margin:2px 3px 0 0}
.run-row{border-bottom:1px solid var(--line);padding:8px 0;font-size:12px}
.backdrop{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40;display:none}.backdrop.open{display:block}
</style></head><body>
<header>
  <a href="#/" class=brand>forgeflow<span class=dim> · BSC</span></a>
  <nav id=nav></nav>
  <div class=spacer></div>
  <span id=round class=pill></span>
  <span id=daemon class=pill>daemon …</span>
  <button id=pausebtn class=pausebtn onclick="ctl(paused?'resume':'pause')"></button>
  <span class=tag id=exec></span>
</header>
<main><div id=view></div></main>
<div class=backdrop id=backdrop onclick="closeDrawer()"></div>
<aside class=drawer id=drawer></aside>
<script>
let paused=false;
const j=(u,o)=>fetch(u,o).then(r=>r.json());
const ctl=(action,params={})=>j('/api/control',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})}).then(refresh);
const kv=o=>Object.entries(o||{}).map(([k,v])=>`<div class=row><span class=k>${k}</span><span>${v}</span></div>`).join('')||'<div class=row><span class=k>—</span></div>';

// each capability's INDEPENDENT run actions (explore/exploit/scout are
// separate entry points — the events auto-chain them, but any can run alone).
const CAP_LABEL={hunt:'Bug hunt',review:'Review',fix:'Fix'};
const CAP_ACTIONS={
  review:[{a:'run_review',t:'▶ poll PRs'}],
  hunt:[{a:'run_hunt',t:'▶ round',base:1},{a:'run_explore',t:'explore'},
        {a:'run_scout',t:'scout'},{a:'run_exploit',t:'exploit',pat:1}],
  fix:[{a:'run_fix',t:'▶ fix triaged',base:1}],
};
const AUTO={review:1,fix:1};                              // enabled == auto-trigger
function renderCaps(s){
  const cont=s.control.hunt_continuous==='1';
  caps.innerHTML=Object.keys(CAP_LABEL).map(c=>{
    const on=s.control['cap_'+c]==='1', acts=CAP_ACTIONS[c]||[];
    const needsBase=acts.some(x=>x.base), needsPat=acts.some(x=>x.pat);
    return `<div class=cap><div class=top><span class=name>${CAP_LABEL[c]}</span>
      <span class="pill ${on?'on':'off'}">${on?'enabled':'disabled'}</span>
      ${AUTO[c]?`<span class=tag>${on?'auto ✓':'auto off'}</span>`:''}
      ${c==='hunt'?`<span class=tag>${cont?'continuous ✓':'manual'}</span>`:''}
      <button onclick="ctl('${on?'disable':'enable'}',{cap:'${c}'})">${on?'disable':'enable'}</button></div>
      ${needsBase?`<input id=base_${c} placeholder=base value="${c==='hunt'&&s.control.hunt_base?s.control.hunt_base:'bishengc/15.0.4'}">`:''}
      ${needsPat?`<input id=pat_${c} placeholder="pattern id, e.g. C1">`:''}
      <div class=ctl>${acts.map(x=>`<button class="${x.t[0]==='▶'?'run':''}"
        onclick="runAct('${c}','${x.a}',${x.pat?1:0})">${x.t}</button>`).join('')}
        ${c==='hunt'?`<button onclick="ctl('continuous',{on:${cont?0:1},base:(document.getElementById('base_hunt')||{}).value||''})">${cont?'continuous ✓':'go continuous'}</button>`:''}</div></div>`;
  }).join('');
}
function runAct(c,a,needsPat){
  const p={}, b=document.getElementById('base_'+c);
  if(b) p.base=b.value;
  if(needsPat){const pt=document.getElementById('pat_'+c); p.pattern=pt?pt.value:'';}
  ctl(a,p);
}

function renderStats(s){
  stats.innerHTML=`
   <div class=card><h2>Tasks</h2>${kv(s.tasks)}</div>
   <div class=card><h2>Findings (bugs)</h2>
     <div class=row><span class=k><b>total</b></span><span><b>${Object.values(s.findings||{}).reduce((a,b)=>a+b,0)}</b></span></div>
     ${kv(s.findings)}</div>
   <div class=card><h2>Methods</h2>${kv(s.methods)}
     <div class=row style="margin-top:6px"><span class=k>PRs opened</span><span>${s.prs}</span></div></div>
   <div class=card><h2>Regions</h2>
     <div class=row><span class=k>total</span><span>${s.regions.total}</span></div>
     <div class=row><span class=k>leased</span><span>${s.regions.leased}</span></div>
     <div class=row><span class=k>cooling</span><span>${s.regions.cooling}</span></div></div>
   <div class=card><h2>Method hit-rate <span class=tag>confirmed / dispatched · bandit signal, not the bug count</span></h2>
     ${(s.top_methods||[]).map(m=>`<div class=row><span class=k>${m.id}${m.status==='exhausted'?' <span class=tag>exhausted</span>':''}</span><span>${m.yield_}/${m.trials}</span></div>`).join('')||kv({})}</div>
   <div class=card><h2>Recent events</h2>
     ${(s.events||[]).map(e=>`<div class=row><span class=k>${e.name}</span><span class=tag>${(e.at||'').slice(11,19)}</span></div>`).join('')||kv({})}</div>`;
}
function renderQueue(q){
  queue.querySelector('tbody').innerHTML = q.length? (
    '<tr><th>id</th><th>kind</th><th>state</th><th>step</th><th>try</th><th>age</th></tr>'+
    q.map(t=>`<tr><td>${t.id}</td><td>${t.kind}</td><td class=st-${t.state}>${t.state}</td>
      <td>${t.step||'—'}</td><td>${t.attempts}</td><td class=tag>${t.age}s</td></tr>`).join('')
  ) : '<tr><td class=tag>idle — no active tasks</td></tr>';
}
const esc=s=>(s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function stepCard(g,st,max){
  return `<div class="block ${st.running?'running':''}"
     onclick="openBlock('${g.name}','${st.name}')">
     <div class=sn>${st.name}</div><div class=bl>${esc(st.block)}</div>
     <div class=ran><span class=badge>${st.ran}</span> ran${st.ms?` · ${st.ms}ms`:''}
       ${st.running?`<span class="badge run">● ${st.running} now</span>`:''}</div></div>`;
}
// top-down tree: one forward step = spine (down), many = branches (fan out);
// terminal outcomes (done/parked/...) are pills; a step reached more than once
// (a DAG join like `merge`) expands at its first path and elsewhere is a dashed
// ref pill. Forward targets are reserved before expanding so joins dedup.
function renderTree(g,name,by,seen,max){
  const st=by[name]; if(!st) return '';
  seen.add(name);
  const groups=new Map();                          // target -> [outcomes]
  Object.entries(st.outcomes||{}).forEach(([o,t])=>{
    if(!groups.has(t)) groups.set(t,[]); groups.get(t).push(o);});
  const fwd=[], pills=[];
  for(const [t,outs] of groups){
    if(by[t] && !seen.has(t)){ seen.add(t); fwd.push([t,outs]); }  // reserve now
    else pills.push([t,outs,by[t]?'ref':'end']);                   // terminal or join
  }
  const pillHtml = pills.length?`<div class=termpills>${pills.map(([t,outs,k])=>
    `<span class="pillt ${k}" ${k==='ref'?`onclick="event.stopPropagation();openBlock('${g.name}','${t}')"`:''}>${outs.join('/')}→${t}</span>`).join('')}</div>`:'';
  let cont='';
  if(fwd.length===1){const [t,outs]=fwd[0];
    cont=`<div class=down><span>${outs.join('/')} ↓</span></div>`+renderTree(g,t,by,seen,max);
  }else if(fwd.length>1){
    cont=`<div class=fork></div><div class=branches>${fwd.map(([t,outs])=>
      `<div class=branch><span class=edge>${outs.join('/')} ↓</span>${renderTree(g,t,by,seen,max)}</div>`).join('')}</div>`;
  }
  return `<div class=treecol>${stepCard(g,st,max)}${pillHtml}${cont}</div>`;
}
// order the workflows of one capability by event flow (A before B if A emits
// an event B consumes); leftovers (cycles) appended.
function orderByFlow(list){
  const byName={},adj={},indeg={};
  list.forEach(g=>{byName[g.name]=g;adj[g.name]=[];indeg[g.name]=0;});
  list.forEach(a=>a.emits.forEach(e=>list.forEach(b=>{
    if(b.name!==a.name && b.consumes.includes(e)){adj[a.name].push(b.name);indeg[b.name]++;}})));
  const q=list.filter(g=>indeg[g.name]===0).map(g=>g.name),out=[],seen=new Set();
  while(q.length){const n=q.shift();if(seen.has(n))continue;seen.add(n);out.push(byName[n]);
    adj[n].forEach(m=>{if(--indeg[m]<=0&&!seen.has(m))q.push(m);});}
  list.forEach(g=>{if(!seen.has(g.name))out.push(g);});
  return out;
}
const CAP_ORDER=[['review','Review pipeline'],['hunt','Bug-hunt campaign'],
                 ['fix','Fix loop'],['other','Other']];
// one workflow's segment: its label + block tree + cross-cap/loop notes.
function wfSeg(g,cap,consMap,capOf,loops){
  const max=Math.max(1,...g.steps.map(s=>s.ran));
  const by={};g.steps.forEach(s=>by[s.name]=s);
  const root=g.steps.length?g.steps[0].name:null;
  const cross=g.emits.map(e=>{const c=(consMap[e]||[]).filter(w=>capOf[w]!==cap&&w!==g.name);
    return c.length?`<b>${e}</b> ⇢ ${c.join(', ')}`:null;}).filter(Boolean);
  return `<div class=seg><a class=seglabel href="#/wf/${g.name}">▸ <b>${g.name}</b></a>
    ${root?renderTree(g,root,by,new Set(),max):''}
    ${cross.length?`<div class=crossnote>also emits ${cross.join(' · ')}</div>`:''}
    ${loops.length?`<div class=crossnote>↺ ${loops.join(' · ')}</div>`:''}</div>`;
}
// walk the WORKFLOW graph: 1 downstream = spine (hand-off, vertical);
// >1 = concurrent spawns drawn side by side (e.g. exploit ∥ scout off explore).
function wfNode(name,byName,cap,consMap,capOf,names,seen){
  seen.add(name);
  const g=byName[name], kids=[], loops=[];
  g.emits.forEach(e=>(consMap[e]||[]).forEach(t=>{
    if(t===name) loops.push(e+' ↺');
    else if(!names.has(t)) {/* cross-cap: shown in wfSeg */}
    else if(seen.has(t)) loops.push(e+'→'+t+' ↺');
    else { seen.add(t); kids.push([e,t]); }
  }));
  const seg=wfSeg(g,cap,consMap,capOf,loops);
  const sub=(t)=>wfNode(t,byName,cap,consMap,capOf,names,seen);
  if(!kids.length) return `<div class=treecol>${seg}</div>`;
  // a LOOPING node keeps running while its spawns run -> put the spawns in the
  // SAME ROW to its right (concurrent), not below it (which reads as sequential).
  if(loops.length){
    const spawns=kids.map(([e,t])=>
      `<div class=spawn><span class=harrow>${e}</span>${sub(t)}</div>`).join('');
    return `<div class=parallelrow><div class=treecol>${seg}</div><div class=spawncol>${spawns}</div></div>`;
  }
  if(kids.length===1){const [e,t]=kids[0];
    return `<div class=treecol>${seg}<div class=hop><div class=bar></div><span class=ev>${e}</span><div class=bar></div></div>${sub(t)}</div>`;
  }
  const br=kids.map(([e,t])=>
    `<div class=branch><span class=edge>${e} ↓</span>${sub(t)}</div>`).join('');
  return `<div class=treecol>${seg}<div class=fork></div><div class=branches>${br}</div></div>`;
}
function pipesHTML(gs, capOnly){
  const emitMap={},consMap={},capOf={},byName={};
  gs.forEach(g=>{capOf[g.name]=g.cap||'other';byName[g.name]=g;
    g.emits.forEach(e=>(emitMap[e]=emitMap[e]||[]).push(g.name));
    g.consumes.forEach(e=>(consMap[e]=consMap[e]||[]).push(g.name));});
  const byCap={};gs.forEach(g=>{const c=g.cap||'other';(byCap[c]=byCap[c]||[]).push(g);});
  let html='';
  for(const [cap,label] of CAP_ORDER){
    if(capOnly && cap!==capOnly) continue;
    const list=byCap[cap];if(!list||!list.length)continue;
    const names=new Set(list.map(g=>g.name)), seen=new Set();
    const entries=list.filter(g=>g.consumes.every(e=>
      !(emitMap[e]||[]).some(w=>names.has(w)&&w!==g.name)));
    const roots=(entries.length?entries:[list[0]]);
    html+=`<div class=capsec><div class=caph>${label}</div><div class=pipe>`;
    roots.concat(list).forEach(r=>{
      if(seen.has(r.name))return;
      html+=`<div class="hop trig"><span class=ev>▼ ${r.consumes[0]||''} · trigger</span><div class=bar></div></div>`;
      html+=wfNode(r.name,byName,cap,consMap,capOf,names,seen);
    });
    html+='</div></div>';
  }
  return html;
}
function closeDrawer(){drawer.classList.remove('open');backdrop.classList.remove('open');}
function openBlock(wf,step){
  backdrop.classList.add('open');drawer.classList.add('open');
  drawer.innerHTML='<button class=x onclick=closeDrawer()>×</button><div class=tag>loading…</div>';
  j(`/api/block?wf=${encodeURIComponent(wf)}&step=${encodeURIComponent(step)}`).then(b=>{
    const sec=(t,h)=>h?`<div class=sec><h4>${t}</h4>${h}</div>`:'';
    drawer.innerHTML=`<button class=x onclick=closeDrawer()>×</button>
      <h3>${esc(b.step)}</h3><div class=sub>${wf} · <span class=bl>${esc(b.block)}</span>${b.timeout_s?` · ${b.timeout_s}s`:''}</div>
      ${b.desc?sec('What it does',`<div class=whatdoes>${esc(b.desc)}</div>`):''}
      ${sec('Outcomes → next', Object.entries(b.outcomes||{}).map(([o,t])=>`<span class=chip>${o} → ${t}</span>`).join('')||'<span class=tag>—</span>')}
      ${b.context&&b.context.length?sec('Context injected',b.context.map(c=>`<span class=chip>${esc(typeof c==='object'?Object.keys(c)[0]:c)}</span>`).join('')):''}
      ${b.params&&Object.keys(b.params).length?sec('Params',`<pre>${esc(JSON.stringify(b.params,null,2))}</pre>`):''}
      ${b.prompt?sec('Prompt'+(b.llm?` (agent: ${b.llm})`:''),`<pre>${esc(b.prompt)}</pre>`):''}
      ${b.schema_def?sec('Output schema'+(b.schema?` (${b.schema})`:''),`<pre>${esc(b.schema_def)}</pre>`):''}
      ${sec('Recent runs',(b.runs||[]).length?b.runs.map(r=>`<div class=run-row>
          <b class=st-${r.outcome==='error'||r.outcome==='timeout'?'parked':'running'}>${r.outcome}</b>
          <span class=tag> · ${r.ms||'?'}ms · ${(r.at||'').slice(5,19)}</span>
          ${r.result?`<pre style="margin-top:4px">${esc(r.result)}</pre>`:''}</div>`).join(''):'<span class=tag>not run yet</span>')}`;
  });
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDrawer();});
// ---- multi-page: a summary HOME, a pipeline page per capability, and a
// ---- detail page per workflow. Client-side hash routing; data cached in
// ---- STATE/WFS and re-rendered every 2s in place.
let STATE=null, WFS=null;
function updateHeader(){
  if(!STATE)return;
  paused=STATE.control.paused==='1';
  daemon.textContent='daemon '+(STATE.daemon.alive?(paused?'paused':'running'):'down');
  daemon.className='pill '+(STATE.daemon.alive&&!paused?'on':'off');
  round.textContent='hunt round '+STATE.round;
  document.getElementById('pausebtn').textContent=paused?'▶ resume all':'⏸ pause all';
  exec.textContent=STATE.daemon.executed+' run'+(STATE.daemon.error?(' · err: '+STATE.daemon.error):'');
}
function renderWfNav(){
  const run={};(STATE?STATE.queue:[]).forEach(t=>{if(t.state==='running')run[t.kind]=(run[t.kind]||0)+1;});
  const byCap={};WFS.forEach(g=>{(byCap[g.cap||'other']=byCap[g.cap||'other']||[]).push(g);});
  let h='';
  for(const [cap,label] of CAP_ORDER){const list=byCap[cap];if(!list)continue;
    h+=`<a class=caplink href="#/cap/${cap}">${label} — pipeline ›</a>`;
    list.forEach(g=>{const r=run[g.name]||0;
      h+=`<a class=wflink href="#/wf/${encodeURIComponent(g.name)}"><span>${g.name}</span>
        <span class=tag>${g.steps.length} blocks${r?` · <b style="color:var(--ok)">● ${r} running</b>`:''} ›</span></a>`;});
  }
  wfnav.innerHTML=h;
}
function renderHome(){
  view.innerHTML=`<div class=h>Overview</div><div class=kpis id=kpis></div>
    <div class=h>Capabilities</div><section class=capgrid id=caps></section>
    <div class=h>Recent activity</div><div class="card feed" id=feed></div>`;
  if(STATE){renderKPIs(STATE);renderCaps(STATE);renderFeed(STATE);}
}
function renderKPIs(s){
  const F=s.findings||{},tot=Object.values(F).reduce((a,b)=>a+b,0),M=s.methods||{},T=s.tasks||{};
  const K=[
    {n:tot,l:'Findings',x:`${F.pr_open||0} in PR · ${F.merged||0} merged`,a:1},
    {n:s.prs||0,l:'PRs opened'},
    {n:M.active||0,l:'Methods',x:`${M.exhausted||0} exhausted`},
    {n:s.regions.total,l:'Regions',x:`${s.regions.leased} leased · ${s.regions.cooling} cooling`},
    {n:T.running||0,l:'Running',x:`${T.pending||0} queued · ${T.failed||0} failed`},
    {n:s.round,l:'Hunt round'}];
  kpis.innerHTML=K.map(k=>`<div class="kpi ${k.a?'accent':''}"><div class=num>${k.n}</div><div class=lbl>${k.l}</div>${k.x?`<div class=sub>${k.x}</div>`:''}</div>`).join('');
}
function renderFeed(s){
  feed.innerHTML=(s.events||[]).map(e=>`<div class=frow><span>${e.name}</span><span class=tag>${(e.at||'').slice(11,19)}</span></div>`).join('')||'<div class=frow><span class=tag>no activity yet</span></div>';
}
function kindCap(kind){const g=(WFS||[]).find(w=>w.name===kind);return g?g.cap:null;}
function qrows(tasks){return tasks.length?('<tr><th>id</th><th>workflow</th><th>state</th><th>step</th><th>age</th></tr>'+tasks.map(t=>`<tr><td>${t.id}</td><td>${t.kind}</td><td class=st-${t.state}>${t.state}</td><td>${t.step||'—'}</td><td class=tag>${t.age}s</td></tr>`).join('')):'<tr><td class=tag>none active</td></tr>';}
function renderCapPage(cap){
  const tasks=(STATE?STATE.queue:[]).filter(t=>kindCap(t.kind)===cap);
  view.innerHTML=`<a class=back href="#/">← home</a>
    ${WFS?pipesHTML(WFS,cap):'<div class=tag>loading…</div>'}
    <div class=h>Queue</div><div class=card><table><tbody>${qrows(tasks)}</tbody></table></div>`;
}
function renderWorkflowPage(name){
  const g=(WFS||[]).find(w=>w.name===name);
  if(!g){view.innerHTML='<a class=back href="#/">← home</a><div class=tag>loading…</div>';return;}
  const by={};g.steps.forEach(s=>by[s.name]=s);
  const root=g.steps.length?g.steps[0].name:null, cap=g.cap||'other';
  const tasks=(STATE?STATE.queue:[]).filter(t=>t.kind===name);
  view.innerHTML=`<a class=back href="#/">← home</a>
    <div class=capsec><div class=caph>${name}
      <span class=tag>${cap} · <a href="#/cap/${cap}">see full pipeline</a></span></div>
      <div class=sub>consumes ${g.consumes.join(', ')||'—'} · emits ${g.emits.join(', ')||'—'}</div>
      <div class=tree>${root?renderTree(g,root,by,new Set(),1):''}</div></div>
    <div class=card><h2>Tasks (${name})</h2><table><tbody>${tasks.length?
      ('<tr><th>id</th><th>state</th><th>step</th><th>age</th></tr>'+tasks.map(t=>
      `<tr><td>${t.id}</td><td class=st-${t.state}>${t.state}</td><td>${t.step||'—'}</td><td class=tag>${t.age}s</td></tr>`).join(''))
      :'<tr><td class=tag>none active</td></tr>'}</tbody></table></div>`;
}
const NAV=[['#/','Home'],['#/cap/review','Review'],['#/cap/hunt','Bug hunt'],['#/cap/fix','Fix']];
function renderNav(){
  const h=location.hash||'#/';
  nav.innerHTML=NAV.map(([href,label])=>
    `<a class="navtab${h===href?' active':''}" href="${href}">${label}</a>`).join('');
}
function render(){
  updateHeader();renderNav();
  const h=location.hash||'#/';
  if(h.startsWith('#/wf/')) renderWorkflowPage(decodeURIComponent(h.slice(5)));
  else if(h.startsWith('#/cap/')) renderCapPage(h.slice(6));
  else renderHome();
}
function refresh(){
  Promise.all([j('/api/state'),j('/api/workflows')]).then(([s,w])=>{STATE=s;WFS=w;render();}).catch(()=>{});
}
window.addEventListener('hashchange',render);
refresh();setInterval(refresh,2000);
</script></body></html>"""


if __name__ == "__main__":
    main()
