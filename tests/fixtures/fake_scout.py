#!/usr/bin/env python3
"""Test fixture: a deterministic ORACLE-SCOUT (claude-cli envelope). Always
returns NO_NEW_METHOD, so a saturated campaign that kicks the scout ends in
bounded steps (proving the saturation->scout->end wiring without a model). A
real scout sometimes returns PROPOSED with new methods that reopen the hunt."""
import json
import sys

sys.stdin.read()
verdict = {"verdict": "NO_NEW_METHOD"}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "fake-scout",
                  "result": "arsenal tapped out.\n```json\n"
                            + json.dumps(verdict) + "\n```"}))
