#!/usr/bin/env python3
"""Deterministic stand-in for the review agents (claude-cli envelope shape).
Prompt-aware: the LENS prompt yields two candidate findings; the REFUTE
prompt confirms the defensible one and rejects the speculative one — so the
pipeline can be exercised (incl. refutation dropping a finding) with no
model cost. A real run just swaps the CLI for `claude`."""
import json
import re
import sys

prompt = sys.stdin.read()


def envelope(result):
    return json.dumps({"type": "result", "subtype": "success",
                       "is_error": False, "session_id": "fake-1",
                       "result": result})


def block(obj):
    return "reasoned about it.\n```json\n" + json.dumps(obj) + "\n```"


if "adversarial reviewer" in prompt or "REFUTE" in prompt:
    # refute mode: keys appear in the candidates context, in id order
    keys = []
    for k in re.findall(r"review-[a-z0-9-]+-\d+", prompt):
        if k not in keys:
            keys.append(k)
    decisions = []
    for i, k in enumerate(keys):
        if i == 0:
            decisions.append({"key": k, "decision": "CONFIRM",
                              "reason": "concrete: untrusted bytes reach a "
                                        "code-execution sink on line shown"})
        else:
            decisions.append({"key": k, "decision": "REJECT",
                              "reason": "cannot construct a reachable failure "
                                        "from the diff; speculative"})
    print(envelope(block({"verdict": "DECIDED", "decisions": decisions})))
elif "_test_clean" in prompt:
    # lens mode, clean case: find nothing
    print(envelope(block({"verdict": "CLEAN", "findings": []})))
else:
    # lens mode: propose two candidates (one solid, one weak)
    print(envelope(block({
        "verdict": "FINDINGS",
        "findings": [
            {"title": "pickle.loads on untrusted coupon bytes (RCE)",
             "severity": "high", "path": "discount.py"},
            {"title": "apply_discount may divide by zero at percent=100",
             "severity": "medium", "path": "discount.py"},
        ]})))
