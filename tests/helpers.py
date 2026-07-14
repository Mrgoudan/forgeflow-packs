"""Shared test scaffolding. Everything is built under tempfile — no scratch
lands in the repo. The engine is located via $ENGINE (default ~/bsd/forgeflow)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PACKS = Path(__file__).resolve().parent.parent
ENGINE = Path(os.environ.get("ENGINE", Path.home() / "bsd" / "forgeflow"))
sys.path.insert(0, str(ENGINE))

FAKE_AGENT = PACKS / "tests" / "fixtures" / "fake_agent.py"


def tmpdir():
    return Path(tempfile.mkdtemp(prefix="ffpacks-test-"))


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd)] + list(a), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def blob(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          stdout=subprocess.PIPE).stdout.decode().strip()


def make_engine(root, pack_dir):
    from forgeflow import config, engine
    return engine.Engine(root, pack=config.load_pack(pack_dir))


# ---- a protocol-faithful fake forge (gitee/gitcode v5 shape) -----------

class FakeForge:
    def __init__(self, pr_number, head_sha, source="feature", target="main"):
        self.comments = []
        outer = self
        pr = {"number": pr_number, "title": "t",
              "head": {"ref": source, "sha": head_sha},
              "base": {"ref": target}}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps([pr]).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))))
                outer.comments.append({"path": self.path, "body": body["body"]})
                self.send_response(201)
                self.end_headers()
                self.wfile.write(json.dumps({"id": 999}).encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def dead_cli(path):
    """A CLI that always fails — the model is unreachable."""
    p = Path(path)
    p.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    p.chmod(0o755)
    return p


# the packs' item lifecycle (now PACK-declared; the engine ships none).
ITEM_STATES = {
    "found": {"triaged", "rejected"}, "triaged": {"fixing", "deferred"},
    "fixing": {"verifying", "deferred", "failed"},
    "verifying": {"pr_open", "fixing", "deferred", "failed"},
    "pr_open": {"in_review", "merged", "failed"},
    "in_review": {"fixing", "merged", "deferred"},
    "merged": set(), "deferred": {"triaged"}, "rejected": set(),
    "failed": {"triaged"},
}
ITEM_STATES_YAML = """item_states:
  found:     [triaged, rejected]
  triaged:   [fixing, deferred]
  fixing:    [verifying, deferred, failed]
  verifying: [pr_open, fixing, deferred, failed]
  pr_open:   [in_review, merged, failed]
  in_review: [fixing, merged, deferred]
  merged:    []
  deferred:  [triaged]
  rejected:  []
  failed:    [triaged]
"""


def pack_db(path):
    """db.connect + the review pack's pack-owned columns (branch, pr_number)."""
    import sys
    from forgeflow import db as _db
    sys.path.insert(0, str(PACKS / "packs" / "review"))
    import migrate as _m
    conn = _db.connect(path)
    _m.add_pack_columns(conn)
    return conn
