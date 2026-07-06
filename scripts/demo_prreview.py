#!/usr/bin/env python3
"""PR-review chain, end to end, against a protocol-faithful fake forge:

    forge.poll_requested -> pr_intake (poll)   -> pr.updated
    pr.updated           -> pr_fetch  (fetch)  -> review.requested
    review.requested     -> review    (agent)  -> review.completed
    review.completed     -> pr_report (egress) -> comment on the PR

Uses a deterministic fake agent (no model cost) and a local HTTP fake
forge. Point prs_url/comment_url/token at a real forge and the SAME pack
runs live. Usage: ENGINE=~/bsd/forgeflow python3 scripts/demo_prreview.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ENGINE = Path(os.environ.get("ENGINE", Path.home() / "bsd" / "forgeflow"))
sys.path.insert(0, str(ENGINE))

from forgeflow import config, db, engine, queue  # noqa: E402


def sh(cwd, *args):
    subprocess.run(list(args), cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_repos(work):
    origin = work / "origin-repo"
    origin.mkdir(parents=True)
    sh(origin, "git", "init", "-q")
    sh(origin, "git", "symbolic-ref", "HEAD", "refs/heads/main")
    sh(origin, "git", "config", "user.email", "d@d.invalid")
    sh(origin, "git", "config", "user.name", "demo")
    (origin / "store.py").write_text("def save(r, db):\n    db[r['id']] = r\n")
    sh(origin, "git", "add", "-A")
    sh(origin, "git", "commit", "-qm", "base")
    sh(origin, "git", "checkout", "-qb", "feature-discount")
    (origin / "discount.py").write_text(
        "import pickle\n\n\ndef apply_discount(price, percent):\n"
        "    return price / (100 - percent)\n\n\n"
        "def load_coupon(raw):\n    return pickle.loads(raw)\n")
    sh(origin, "git", "add", "-A")
    sh(origin, "git", "commit", "-qm", "discount feature")
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(origin),
                              stdout=subprocess.PIPE).stdout.decode().strip()
    sh(origin, "git", "checkout", "-q", "main")
    clone = work / "clone"
    sh(work, "git", "clone", "-q", str(origin), str(clone))
    return origin, clone, head_sha


class FakeForge:
    def __init__(self, head_sha):
        self.comments = []
        outer = self
        pr = {"number": 7, "title": "add discount feature",
              "head": {"ref": "feature-discount", "sha": head_sha},
              "base": {"ref": "main"}}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = json.dumps([pr]).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))))
                outer.comments.append({"path": self.path, "body": body["body"]})
                self.send_response(201)
                self.end_headers()
                self.wfile.write(json.dumps({"id": 555}).encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def main():
    work = HERE / "demo-pr-run"
    shutil.rmtree(str(work), ignore_errors=True)
    work.mkdir()
    origin, clone, head_sha = build_repos(work)
    forge = FakeForge(head_sha)

    secrets = work / "secrets.env"
    secrets.write_text("FORGE_TOKEN_DEMO=tok-demo-123\n")
    secrets.chmod(0o600)
    os.environ["FORGEFLOW_SECRETS"] = str(secrets)
    os.environ["FORGE_WRITE"] = "1"

    fake_agent = HERE / "scripts" / "fake_review_agent.py"
    (HERE / "review" / "project.yaml").write_text("""\
name: review
paths: {{ repo: {repo} }}
tools: {{ git: {{ path: git }} }}
workflows: [workflows]
blocks:    [blocks/reviewblocks.py, blocks/forge.py]
prompts: {{ review: prompts/review.md }}
schemas: {{ review_findings: schemas/review_findings.yaml }}
agents:
  review: {{ backend: claude-cli, cli: {cli} }}
params:
  prs_url: "{forge}/repos/demo/pulls?state=open"
  comment_url: "{forge}/repos/demo/pulls/{{payload.request.pr}}/comments"
  forge_auth: {{ token_ref: DEMO, style: query, name: access_token }}
  deny_patterns: ["BEGIN [A-Z]+ PRIVATE KEY"]
""".format(repo=clone, cli=fake_agent, forge=forge.url))

    eng = engine.Engine(work / "ff", pack=config.load_pack(HERE / "review"))
    print("orchestration:", json.dumps(eng.subscriptions, indent=1))
    db.emit_event(eng.conn, "forge.poll_requested", {"tick": 1}, eng.subscriptions)
    n = eng.run_until_idle()
    print("\nexecuted %d task(s)" % n)

    print("\n-- tasks --")
    for t in eng.conn.execute("SELECT id, kind, state FROM tasks ORDER BY id"):
        print(" ", dict(t))
    print("-- findings --")
    for f in eng.conn.execute("SELECT key, state, severity, title FROM findings"):
        print(" ", dict(f))
    print("-- egress --")
    for e in eng.conn.execute("SELECT id, kind, target, forge_id FROM egress"):
        print(" ", dict(e))
    print("-- comment the fake forge received --")
    print("  path:", forge.comments[0]["path"])
    print("  " + forge.comments[0]["body"].replace("\n", "\n  "))

    # replay: poll again — nothing new happens anywhere down the chain
    db.emit_event(eng.conn, "forge.poll_requested", {"tick": 2}, eng.subscriptions)
    n2 = eng.run_until_idle()
    total = eng.conn.execute("SELECT count(*) c FROM tasks").fetchone()["c"]
    print("\nreplay poll: executed %d task(s) (just the poll); tasks total %d;"
          " comments still %d" % (n2, total, len(forge.comments)))

    assert n == 4 and n2 == 1 and len(forge.comments) == 1
    assert "tok-demo-123" in forge.comments[0]["path"]      # query auth flowed
    states = [r["state"] for r in eng.conn.execute("SELECT state FROM tasks")]
    assert all(s == "done" for s in states), states
    print("\nPR REVIEW CHAIN: OK")


if __name__ == "__main__":
    main()
