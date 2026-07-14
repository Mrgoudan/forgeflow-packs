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
#   ./run-bsc.sh dash [--port 8787]   # control room: stats + queue + block maps
#   ./run-bsc.sh port                 # one-time vault -> db knowledge port
set -euo pipefail

PACK_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE="${ENGINE:-$HOME/bsd/forgeflow}"
SECRETS="${FORGEFLOW_SECRETS:-$PACK_DIR/../../config/secrets.env}"
FF_ROOT="${FF_ROOT:-$PACK_DIR/../../run}"
mkdir -p "$PACK_DIR/../../run"        # paths.data_root anchor must exist at load (validate)

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

# LIVE deployment: egress posts for real (review comments, issue reports, fix
# PRs). Set FORGE_WRITE=0 before launching for a dry run (archive only).
export FORGE_WRITE="${FORGE_WRITE:-1}"

# BSC review is all-domestic (GLM bigmodel + gitcode). The machine's proxy
# is for international egress and would HANG these endpoints — drop it so
# the agent and forge calls go direct. (Override by exporting NO_PROXY_UNSET=1.)
if [ -z "${NO_PROXY_UNSET:-}" ]; then
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
fi

# the control room is the daemon + a web UI (stats/queue/block maps/controls);
# it runs agent tasks itself, so it needs the same sourced env as `run`.
if [ "${1:-}" = "dash" ]; then
  shift
  exec python3 "$PACK_DIR/dashboard.py" --root "$FF_ROOT" --pack "$PACK_DIR" "$@"
fi
if [ "${1:-}" = "port" ]; then
  shift
  exec python3 "$PACK_DIR/port.py" --root "$FF_ROOT" --pack "$PACK_DIR" "$@"
fi

# db <-> git: export the DB's knowledge to a versionable SQL text file (the
# living DB is the source of truth; this is its portable projection), and
# rebuild a DB from one. Default location is the sibling data/ dir (its own
# repo, like vault/). FF_DATA overrides.
DATA_DIR="${FF_DATA:-$PACK_DIR/../../data}"
if [ "${1:-}" = "export" ]; then
  shift
  exec python3 "$PACK_DIR/scripts/db_export.py" \
    --db "$FF_ROOT/state/forgeflow.db" --out "$DATA_DIR/knowledge" --pack "$PACK_DIR" "$@"
fi
if [ "${1:-}" = "import" ]; then
  shift
  exec python3 "$PACK_DIR/scripts/db_import.py" \
    --db "$FF_ROOT/state/forgeflow.db" --dir "$DATA_DIR/knowledge" "$@"
fi

# pack-owned column migrations (the engine adds tables, never columns)
if [ -f "$FF_ROOT/state/forgeflow.db" ]; then
  python3 "$PACK_DIR/scripts/migrate_db.py" --db "$FF_ROOT/state/forgeflow.db"
fi

exec python3 -m forgeflow --root "$FF_ROOT" --pack "$PACK_DIR" "$@"
