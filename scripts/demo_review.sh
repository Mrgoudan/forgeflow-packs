#!/usr/bin/env bash
# End-to-end review demo: builds a toy repo with a deliberately buggy
# branch, points the review pack at it, and runs the REAL agent review.
# Usage: ENGINE=/path/to/forgeflow ./scripts/demo_review.sh [workdir]
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ENGINE:-$HOME/bsd/forgeflow}"
WORK="${1:-$HERE/demo-run}"
export PYTHONPATH="$ENGINE"

rm -rf "$WORK"
mkdir -p "$WORK"

# --- toy repo with a buggy feature branch ------------------------------
REPO="$WORK/toy-repo"
mkdir -p "$REPO"
git -C "$REPO" init -q -b main 2>/dev/null || { git -C "$REPO" init -q; git -C "$REPO" checkout -qb main; }
git -C "$REPO" config user.email demo@demo.invalid
git -C "$REPO" config user.name demo
cat > "$REPO/store.py" <<'EOF'
def save(record, db):
    db[record["id"]] = record
EOF
git -C "$REPO" add -A && git -C "$REPO" commit -qm "base"

git -C "$REPO" checkout -qb feature-discount
cat > "$REPO/discount.py" <<'EOF'
import pickle


def apply_discount(price, percent):
    return price / (100 - percent)


def load_coupon(raw_bytes):
    return pickle.loads(raw_bytes)
EOF
git -C "$REPO" add -A && git -C "$REPO" commit -qm "add discount feature"
git -C "$REPO" checkout -q main

# --- machine-local pack config ------------------------------------------
cat > "$HERE/review/project.yaml" <<EOF
name: review
paths: { repo: $REPO }
tools: { git: { path: git, version_cmd: ["--version"] } }
workflows: [workflows]
blocks:    [blocks/reviewblocks.py, blocks/providers.py]
prompts: { review: prompts/review.md }
schemas: { review_findings: schemas/review_findings.yaml }
agents:
  review: { backend: claude-cli }
EOF

# --- run it ---------------------------------------------------------------
python3 -m forgeflow --root "$WORK/ff" --pack "$HERE/review" validate
python3 -m forgeflow --root "$WORK/ff" --pack "$HERE/review" emit review.requested \
    --data '{"branch": "feature-discount", "base": "main"}' --drive
python3 -m forgeflow --root "$WORK/ff" status

echo
echo "--- findings rows ---"
python3 - "$WORK/ff/state/forgeflow.db" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1]); conn.row_factory = sqlite3.Row
for r in conn.execute("SELECT id, key, state, severity, title FROM findings"):
    print(dict(r))
EOF
