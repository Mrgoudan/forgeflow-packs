#!/usr/bin/env python3
"""Test fixture: a deterministic bug EXPLORER (claude-cli envelope). Always
returns NO_NEW_PATTERN so a hunt loop dries out and saturates in bounded
steps — proving the auto-swap + cooldown + termination mechanics without a
model. A real explorer sometimes returns CONFIRMED_NEW with a probe."""
import json
import sys

sys.stdin.read()
verdict = {"verdict": "NO_NEW_PATTERN",
           "note": {"object": "region.cpp", "invariant": "must hold",
                    "candidates": ["reachability", "symmetry", "composition"]}}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "fake-explore",
                  "result": "read the region.\n```json\n"
                            + json.dumps(verdict) + "\n```"}))
