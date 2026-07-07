#!/usr/bin/env bash
# Restore the reviewed repo to its base branch after a review.
#   restore_base.sh <repo> <base_branch>
set -euo pipefail
git -C "$1" checkout -q "$2"
echo "restored $1 to $2"
