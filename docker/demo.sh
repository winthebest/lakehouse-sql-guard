#!/usr/bin/env bash
# Run the Spark demo on the lakehouse stack: starts Lakekeeper, the S3-compatible store and
# Spark (docker/compose.lakehouse.yml), runs docker/demo.py in the `runner` container, and
# removes the stack's containers and volumes afterwards. Needs only Docker.
#
#   docker/demo.sh            run the demo, then clean up
#   SQLGUARD_KEEP_STACK=1 docker/demo.sh     leave the stack running 
#                                              (then stop it: see the last line of docker/run-integration.sh)
#
# The compose project is `sqlguard-lakehouse`; nothing else on the machine is touched. Do not run
# this while docker/run-integration.sh is running: they share the project.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(docker compose -p sqlguard-lakehouse -f "$root/docker/compose.lakehouse.yml")

cleanup() {
  if [[ "${SQLGUARD_KEEP_STACK:-0}" != "1" ]]; then
    "${compose[@]}" --profile '*' down -v --remove-orphans
  fi
}
trap cleanup EXIT

"${compose[@]}" up -d --wait spark
"${compose[@]}" run --rm runner uv run --frozen --extra spark --extra mcp python docker/demo.py "$@"
