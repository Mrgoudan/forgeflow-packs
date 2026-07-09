#!/usr/bin/env bash
# Keep the base clang current: checkout the base branch, PULL latest, rebuild.
# After this, build/bin/clang IS base clang; the record step snapshots its
# probe outputs (the base baseline) keyed by the new base rev.
#   refresh_base.sh <repo> <build_dir> <base_branch>
set -euo pipefail
REPO="$1"; BUILD="$2"; BASE="$3"
JOBS="${BUILD_JOBS:-6}"          # cap parallelism (see build_clang.sh: avoid swap/freeze)
git -C "$REPO" checkout -q "$BASE"
git -C "$REPO" pull --ff-only 2>&1 | tail -2 || echo "(pull skipped/failed — building current base)"
ninja -j "$JOBS" -C "$BUILD" clang
echo "base clang rebuilt at $(git -C "$REPO" rev-parse --short HEAD) (-j $JOBS)"
