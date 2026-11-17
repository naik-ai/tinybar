#!/bin/bash
# Makes a folder that looks like backend/ to the Models workspace, whose `tinybar-models serve` and
# `stop` run the stand-in gateway instead of the real one. Point the workspace's backend folder at it
# to check Start and Stop without touching the real gateway (port 18900) or loading a model.
#
#   Tests/StandIn/make-fake-backend.sh <folder> [<state folder>]
#
# The state folder (default $TMPDIR/tinybar-standin) holds the stand-in's key file, api-key.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="${1:?usage: make-fake-backend.sh <folder> [<state folder>]}"
state="${2:-${TMPDIR:-/tmp}/tinybar-standin}"
mkdir -p "$dest/.venv/bin"
cp "$here/../../backend/registry.json" "$dest/registry.json"
# Start runs the CLI only from a folder that looks like the backend (pyproject.toml beside .venv).
printf '[project]\nname = "tinybar-models-standin"\nversion = "0"\n' > "$dest/pyproject.toml"
cat > "$dest/.venv/bin/tinybar-models" <<EOF
#!/bin/sh
# Stand-in shim (Tests/StandIn): never the real gateway.
case "\$1" in
  serve) shift; exec /usr/bin/python3 -I "$here/standin_gateway.py" serve --state "$state" "\$@" ;;
  stop) exec /usr/bin/python3 -I "$here/standin_gateway.py" stop --state "$state" ;;
  login) shift; exec /usr/bin/python3 -I "$here/standin_gateway.py" login "\$@" --state "$state" ;;
  start) exec /usr/bin/python3 -I "$here/standin_gateway.py" start --state "$state" ;;
  *) echo "stand-in shim: '\$1' is not supported" >&2; exit 2 ;;
esac
EOF
chmod 755 "$dest/.venv/bin/tinybar-models"
echo "fake backend in $dest (state $state)"
