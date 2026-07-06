#!/usr/bin/env python3
"""Minimal /v1/embeddings sidecar for a local sentence-transformers model.

Loads the model ONCE, serves the de-facto embeddings protocol, so the
forgeflow engine (which never imports ML runtimes) reaches it through its
standard api-backed models config:

    models:
      bertish: { base_url: "http://127.0.0.1:7997/v1", model: all-MiniLM-L6-v2 }

Usage:  python3 embed_server.py [--port 7997] [--model all-MiniLM-L6-v2]
        EMBED_DEVICE=cpu (default) or cuda
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7997)
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    args = ap.parse_args()

    from sentence_transformers import SentenceTransformer
    device = os.environ.get("EMBED_DEVICE", "cpu")
    model = SentenceTransformer(args.model, device=device)
    dim = model.get_sentence_embedding_dimension()
    print("ready: %s dim=%d device=%s port=%d"
          % (args.model, dim, device, args.port), flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):          # health check
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"model": args.model, "dim": dim}).encode())

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))))
                texts = body.get("input") or []
                if isinstance(texts, str):
                    texts = [texts]
                vecs = model.encode(texts, normalize_embeddings=True,
                                    convert_to_numpy=True)
                data = [{"index": i, "embedding": [float(x) for x in v]}
                        for i, v in enumerate(vecs)]
                payload = json.dumps({"model": args.model, "data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:          # malformed request: client error
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
