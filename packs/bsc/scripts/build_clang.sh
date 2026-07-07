#!/usr/bin/env bash
# Evidence-gate build: put the pack's clang at the PR's state, incrementally.
# Detached checkout of the PR head sha (works even though a review worktree
# has the branch), then ninja the clang target. Leaves build/bin/clang
# reflecting the PR so the probe sweep tests PR behavior.
#   build_clang.sh <repo> <build_dir> <head_sha>
set -euo pipefail
REPO="$1"; BUILD="$2"; SHA="$3"
git -C "$REPO" checkout -q --detach "$SHA"
ninja -C "$BUILD" clang
echo "built PR clang at $BUILD/bin/clang (detached $SHA)"
