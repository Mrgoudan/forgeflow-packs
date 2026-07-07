#!/usr/bin/env bash
# Launch the BSC reviewer with secrets sourced from one env file. Sourcing
# (set -a) puts ANTHROPIC_* in the environment for the agent's env_keys,
# and exporting FORGEFLOW_SECRETS points load_secrets() at the same file
# for FORGE_TOKEN_*. Everything from one 0600 file.
#
# Usage:
#   ./run-bsc.sh validate
#   ./run-bsc.sh emit forge.poll_requested --data '{}' --drive
#   ./run-bsc.sh run
set -euo pipefail

PACK_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE="${ENGINE:-$HOME/bsd/forgeflow}"
SECRETS="${FORGEFLOW_SECRETS:-$PACK_DIR/../config/secrets.env}"
FF_ROOT="${FF_ROOT:-$PACK_DIR/.run}"

if [ ! -f "$SECRETS" ]; then
  echo "missing $SECRETS — cp ../secrets.env.example $PACK_DIR/secrets.env; fill it; chmod 600" >&2
  exit 1
fi
perm=$(stat -c '%a' "$SECRETS")
if [ "$perm" != "600" ]; then
  echo "refusing: $SECRETS is mode $perm, must be 600 (chmod 600 it)" >&2
  exit 1
fi

set -a; . "$SECRETS"; set +a
export FORGEFLOW_SECRETS="$SECRETS"
export PYTHONPATH="$ENGINE${PYTHONPATH:+:$PYTHONPATH}"

exec python3 -m forgeflow --root "$FF_ROOT" --pack "$PACK_DIR" "$@"
