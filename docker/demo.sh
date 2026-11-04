#!/usr/bin/env bash
# Run the Spark demo against the Docker stack:
#
#   docker compose -f docker/docker-compose.yml up -d --wait
#   docker/demo.sh
#   docker compose -f docker/docker-compose.yml down -v
#
# Needs only `uv`; the demo runs on the host and talks to Spark over Spark Connect.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

exec uv run --extra spark --extra mcp python docker/demo.py "$@"
