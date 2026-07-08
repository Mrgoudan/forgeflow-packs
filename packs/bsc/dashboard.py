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
_DEFAULTS = {"paused": "0", "cap_hunt": "1", "cap_review": "1", "cap_fix": "1"}


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

    def loop(self):
        idle = 1.0
        while not self.stop.is_set():
            if flag(self.conn, "paused") == "1":
                time.sleep(idle)
                continue
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
    top_methods = [dict(id=r["id"], trials=r["trials"], yield_=r["verified_yield"])
                   for r in conn.execute(
                       "SELECT id, trials, verified_yield FROM methods"
                       " WHERE status='active' ORDER BY verified_yield DESC,"
                       " trials DESC LIMIT 6")]
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


def block_detail(conn, pack, workflow_dirs, wf_name, step_name):
    """Everything behind a block card: its wiring, its PROMPT (for agent
    steps), its output schema, and its recent runs."""
    wf = _parse_workflows(workflow_dirs).get(wf_name)
    if not wf:
        return {"error": "no such workflow"}
    st = next((s for s in wf["steps"] if s["name"] == step_name), None)
    if not st:
        return {"error": "no such step"}
    out = {"workflow": wf_name, "step": step_name, "block": st["block"],
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
        db.emit_event(conn, "hunt.round_requested", {"base": base}, subs)
    elif action == "run_review":
        db.emit_event(conn, "forge.poll_requested", {}, subs)
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
:root{--bg:#0e1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{display:flex;align-items:center;gap:12px;padding:12px 18px;
border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:15px;margin:0;font-weight:600}.spacer{flex:1}
.pill{padding:2px 9px;border-radius:12px;border:1px solid var(--line);font-size:11px}
.pill.on{color:var(--ok);border-color:var(--ok)}.pill.off{color:var(--warn);border-color:var(--warn)}
main{padding:16px;display:grid;gap:16px;max-width:1200px;margin:0 auto}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card h2{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:0 0 8px}
.big{font-size:26px;font-weight:600}.row{display:flex;justify-content:space-between;gap:8px;padding:1px 0}
.row .k{color:var(--dim)}button{font:inherit;background:#21262d;color:var(--fg);
border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer}
button:hover{border-color:var(--accent)}button.run{color:var(--ok);border-color:#238636}
button.stop{color:var(--warn)}.caps{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.cap{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.cap .top{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.cap .name{font-weight:600;text-transform:capitalize}.cap .ctl{display:flex;gap:6px;margin-top:8px}
input{font:inherit;background:#0d1117;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:3px 6px;width:130px}
.wf{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;overflow-x:auto}
.wf h3{margin:0 0 2px;font-size:13px}.wf .sub{color:var(--dim);font-size:11px;margin-bottom:10px}
.blocks{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(160px,1fr))}
.block{background:#0d1117;border:1px solid var(--line);border-radius:7px;padding:8px 10px;
cursor:pointer;transition:transform .08s;position:relative;overflow:hidden}
.block:hover{transform:translateY(-2px);border-color:var(--accent)}
.block .sn{font-weight:600}.block .bl{color:var(--accent);font-size:11px;word-break:break-all}
.block .ran{color:var(--dim);font-size:11px;margin-top:5px}
.block .out{font-size:10px;color:var(--dim);margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.block.running{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok)}
.block.running::after{content:'';position:absolute;top:6px;right:6px;width:8px;height:8px;
border-radius:50%;background:var(--ok);animation:pulse 1.1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.badge{display:inline-block;min-width:18px;text-align:center;background:#21262d;border-radius:10px;
padding:0 6px;color:var(--ok);font-size:11px}.badge.run{background:#0f2f1a}
/* detail drawer */
.drawer{position:fixed;top:0;right:0;height:100%;width:min(560px,95vw);background:var(--card);
border-left:1px solid var(--line);transform:translateX(100%);transition:transform .18s;
z-index:20;overflow-y:auto;padding:16px 18px}
.drawer.open{transform:none}.drawer h3{margin:0 0 2px}.drawer .x{position:absolute;top:12px;right:14px;
background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer}
.drawer .sec{margin-top:14px}.drawer .sec h4{font-size:11px;text-transform:uppercase;
letter-spacing:.05em;color:var(--dim);margin:0 0 5px}
pre{background:#0d1117;border:1px solid var(--line);border-radius:6px;padding:10px;
overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-size:12px;margin:0}
.chip{display:inline-block;background:#21262d;border:1px solid var(--line);border-radius:12px;
padding:1px 9px;font-size:11px;margin:2px 3px 0 0}
.run-row{border-bottom:1px solid var(--line);padding:6px 0;font-size:12px}
.backdrop{position:fixed;inset:0;background:#0008;z-index:15;display:none}.backdrop.open{display:block}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:3px 6px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:11px}.st-running{color:var(--accent)}
.st-parked{color:var(--warn)}.st-retry_wait{color:var(--warn)}.st-pending{color:var(--fg)}
.tag{font-size:10px;color:var(--dim)}
</style></head><body>
<header>
  <h1>forgeflow · BSC control room</h1>
  <span id=daemon class=pill>daemon …</span>
  <span id=round class=pill></span>
  <div class=spacer></div>
  <button id=pausebtn onclick="ctl(paused?'resume':'pause')"></button>
  <span class=tag id=exec></span>
</header>
<main>
  <section class=caps id=caps></section>
  <section class=grid id=stats></section>
  <div class=card><h2>Queue (active tasks)</h2><table id=queue><tbody></tbody></table></div>
  <div id=workflows></div>
</main>
<div class=backdrop id=backdrop onclick="closeDrawer()"></div>
<aside class=drawer id=drawer></aside>
<script>
let paused=false;
const j=(u,o)=>fetch(u,o).then(r=>r.json());
const ctl=(action,params={})=>j('/api/control',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})}).then(refresh);
const kv=o=>Object.entries(o||{}).map(([k,v])=>`<div class=row><span class=k>${k}</span><span>${v}</span></div>`).join('')||'<div class=row><span class=k>—</span></div>';

const CAP_META={hunt:{run:'run_hunt',label:'Bug hunt'},review:{run:'run_review',label:'Review'},fix:{run:'run_fix',label:'Fix'}};
function renderCaps(s){
  caps.innerHTML=Object.keys(CAP_META).map(c=>{
    const on=s.control['cap_'+c]==='1', m=CAP_META[c];
    const needsBase=(c==='hunt'||c==='fix');
    return `<div class=cap><div class=top><span class=name>${m.label}</span>
      <span class="pill ${on?'on':'off'}">${on?'enabled':'disabled'}</span></div>
      <div class=ctl>
        ${needsBase?`<input id=base_${c} placeholder=base value="bishengc/15.0.4">`:''}
        <button class=run onclick="run('${c}')">▶ run</button>
        <button onclick="ctl('${on?'disable':'enable'}',{cap:'${c}'})">${on?'disable':'enable'}</button>
      </div></div>`;
  }).join('');
}
function run(c){const b=document.getElementById('base_'+c);
  ctl(CAP_META[c].run,b?{base:b.value}:{});}

function renderStats(s){
  stats.innerHTML=`
   <div class=card><h2>Tasks</h2>${kv(s.tasks)}</div>
   <div class=card><h2>Findings</h2>${kv(s.findings)}</div>
   <div class=card><h2>Methods</h2>${kv(s.methods)}
     <div class=row style="margin-top:6px"><span class=k>PRs opened</span><span>${s.prs}</span></div></div>
   <div class=card><h2>Regions</h2>
     <div class=row><span class=k>total</span><span>${s.regions.total}</span></div>
     <div class=row><span class=k>leased</span><span>${s.regions.leased}</span></div>
     <div class=row><span class=k>cooling</span><span>${s.regions.cooling}</span></div></div>
   <div class=card><h2>Top methods (yield/trials)</h2>
     ${(s.top_methods||[]).map(m=>`<div class=row><span class=k>${m.id}</span><span>${m.yield_}/${m.trials}</span></div>`).join('')||kv({})}</div>
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
function heat(ran,max){if(!ran)return'';const t=Math.min(1,ran/(max||1));
  return `background:rgba(88,166,255,${(0.06+t*0.20).toFixed(2)})`;}
function renderWorkflows(gs){
  workflows.innerHTML=gs.map(g=>{
    const max=Math.max(1,...g.steps.map(s=>s.ran));
    return `<div class=wf>
    <h3>${g.name} <span class=tag>${g.cap||''}</span></h3>
    <div class=sub>consumes ${g.consumes.join(', ')||'—'} · emits ${g.emits.join(', ')||'—'}</div>
    <div class=blocks>${g.steps.map(st=>`
      <div class="block ${st.running?'running':''}" style="${heat(st.ran,max)}"
           onclick="openBlock('${g.name}','${st.name}')">
        <div class=sn>${st.name}</div><div class=bl>${esc(st.block)}</div>
        <div class=ran><span class=badge>${st.ran}</span> ran${st.ms?` · ${st.ms}ms`:''}
          ${st.running?`<span class="badge run">● ${st.running} now</span>`:''}</div>
        <div class=out>${Object.entries(st.outcomes).map(([o,t])=>`${o}→${t}`).join('  ')||'—'}</div>
      </div>`).join('')}</div></div>`;}).join('');
}
function closeDrawer(){drawer.classList.remove('open');backdrop.classList.remove('open');}
function openBlock(wf,step){
  backdrop.classList.add('open');drawer.classList.add('open');
  drawer.innerHTML='<button class=x onclick=closeDrawer()>×</button><div class=tag>loading…</div>';
  j(`/api/block?wf=${encodeURIComponent(wf)}&step=${encodeURIComponent(step)}`).then(b=>{
    const sec=(t,h)=>h?`<div class=sec><h4>${t}</h4>${h}</div>`:'';
    drawer.innerHTML=`<button class=x onclick=closeDrawer()>×</button>
      <h3>${esc(b.step)}</h3><div class=sub>${wf} · <span class=bl>${esc(b.block)}</span>${b.timeout_s?` · ${b.timeout_s}s`:''}</div>
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
function refresh(){
  j('/api/state').then(s=>{
    paused=s.control.paused==='1';
    daemon.textContent='daemon '+(s.daemon.alive?(paused?'paused':'running'):'down');
    daemon.className='pill '+(s.daemon.alive&&!paused?'on':'off');
    round.textContent='hunt round '+s.round;
    document.getElementById('pausebtn').textContent=paused?'▶ resume all':'⏸ pause all';
    exec.textContent=s.daemon.executed+' tasks run'+(s.daemon.error?(' · last err: '+s.daemon.error):'');
    renderCaps(s);renderStats(s);renderQueue(s.queue);
  });
  j('/api/workflows').then(renderWorkflows);
}
refresh();setInterval(refresh,2000);
</script></body></html>"""


if __name__ == "__main__":
    main()
