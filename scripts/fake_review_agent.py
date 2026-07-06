#!/usr/bin/env python3
"""Deterministic stand-in for the review agent (claude-cli envelope shape).
Lets the PR-review chain be exercised end-to-end without model cost."""
import json
import sys

sys.stdin.read()
verdict = {
    "verdict": "FINDINGS",
    "findings": [
        {"title": "pickle.loads on untrusted coupon bytes (RCE)",
         "severity": "high", "path": "discount.py"},
        {"title": "apply_discount divides by zero at percent=100",
         "severity": "medium", "path": "discount.py"},
    ],
}
result = "reviewed the diff.\n```json\n" + json.dumps(verdict) + "\n```"
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "fake-1", "result": result}))
