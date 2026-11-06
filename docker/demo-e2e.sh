#!/usr/bin/env bash
# End-to-end demo on real infrastructure, in one command, cleaned up afterwards.
#
#   docker/demo-e2e.sh                         scripted agent (no model, no key)
#   docker/demo-e2e.sh --agent claude          Claude Code headless drives the agent turns (see below)
#   docker/demo-e2e.sh --screenshots DIR       also save screenshots of the approval UI in DIR
#   SQLGUARD_KEEP_STACK=1 docker/demo-e2e.sh   leave the stack up (remove it: see the last line)
#
# It starts Lakekeeper (an Iceberg REST catalog) on an S3-compatible store from
# docker/compose.lakehouse.yml, and one long-lived container of the `runner` service on the same
# network. Then docker/demo-e2e/story.py runs the story:
#
#   1. Leakage: nightly Iceberg commits with late rows are written to Lakekeeper, `dbt build`
#      builds a training set that leaks through an intermediate model, `sqlguard leakage dbt`
#      finds the leak there and suggests the fix, the fix is applied and the check is clean.
#   2. Safety: an agent talks MCP to `sqlguard-mcp`. A delete of production data is held and a
#      reviewer rejects it in the approval UI; a narrower request is approved and published by
#      snapshot id; a write by someone else between review and click leaves an action `stale`;
#      a rollback is approved and done.
#
# Needs Docker and Python 3.10+ (standard library only) on the host. The compose project is
# `sqlguard-lakehouse`; nothing else on the machine is touched. Do not run it while
# docker/run-integration.sh or docker/demo.sh run: they share the project (this script refuses).
# On exit, also after a failure or Ctrl-C, the runner container, the project's containers,
# networks and volumes, and the temporary state directory are removed. Only the image stays.
#
# Ports, all on 127.0.0.1: 19000/19001 (object store), 18181 (Lakekeeper), 18765 (approval UI).
# If one is taken the script says by whom and stops; it never kills anything.
#
# --agent claude runs `claude -p` on the host, once per agent turn, with the guard as its only
# tools: --tools "" (no built-in tool), --allowedTools for the MCP tools of the task,
# --disallowedTools for the built-ins and the maintenance tools, --permission-mode dontAsk, and
# --strict-mcp-config. The MCP server is started inside the runner with `docker exec -i`. It
# needs `claude` logged in (or ANTHROPIC_API_KEY) and spends model tokens.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project=sqlguard-lakehouse
compose=(docker compose -p "$project" -f "$root/docker/compose.lakehouse.yml")
runner=sqlguard-lakehouse-demo-runner
ui_port=18765
agent=scripted
screenshots=""
state=""
started=0

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d;s/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) agent="${2:?--agent needs scripted or claude}"; shift 2 ;;
    --screenshots) screenshots="${2:?--screenshots needs a directory}"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done
case "$agent" in scripted | claude) ;; *) echo "--agent must be scripted or claude" >&2; exit 2 ;; esac

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "${SQLGUARD_KEEP_STACK:-0}" == "1" ]]; then
    echo
    echo "stack left running. Remove it with:"
    echo "  docker rm -f $runner; docker compose -p $project -f docker/compose.lakehouse.yml --profile '*' down -v --remove-orphans"
    exit "$status"
  fi
  if [[ "$started" == "1" ]]; then
    echo
    echo "cleaning up: runner container, the $project containers and volumes, temporary state"
    # root in the container owns what it wrote under the bind mount: empty it from inside
    docker exec "$runner" sh -c 'rm -rf /state/* /state/.[!.]*' >/dev/null 2>&1 || true
    docker rm -f "$runner" >/dev/null 2>&1 || true
    "${compose[@]}" --profile '*' down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  [[ -n "$state" ]] && rm -rf "$state"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "the Docker daemon is not reachable" >&2; exit 2; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || { echo "python3 (3.10 or newer) is needed on the host" >&2; exit 2; }
if [[ "$agent" == "claude" ]]; then
  command -v claude >/dev/null || { echo "--agent claude needs the claude CLI on the host" >&2; exit 2; }
fi

if [[ -n "$("${compose[@]}" --profile '*' ps -aq 2>/dev/null)" ]]; then
  echo "the $project project already has containers (is docker/run-integration.sh or docker/demo.sh running?)." >&2
  echo "stop them first: docker compose -p $project -f docker/compose.lakehouse.yml --profile '*' down -v" >&2
  exit 2
fi
if command -v lsof >/dev/null; then
  for port in 19000 19001 18181 "$ui_port"; do
    if owner="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sed -n '2p')" && [[ -n "$owner" ]]; then
      echo "port $port on 127.0.0.1 is in use: $owner" >&2
      echo "stop that process if it is yours (see: lsof -i :$port); this script will not." >&2
      exit 2
    fi
  done
fi

state="$(mktemp -d "${TMPDIR:-/tmp}/sqlguard-demo.XXXXXX")"
[[ -n "$screenshots" ]] && mkdir -p "$screenshots"

# Run a command quietly; show its output only when it fails.
quiet() {
  local log="$state/setup.log"
  if ! "$@" >"$log" 2>&1; then
    cat "$log" >&2
    return 1
  fi
}

begin=$SECONDS
started=1
echo "== starting the stack: Lakekeeper on an S3-compatible store (compose project $project)"
quiet "${compose[@]}" build runner
quiet "${compose[@]}" up -d --wait lakekeeper
quiet "${compose[@]}" run --rm lakekeeper-bootstrap
rest=http://lakekeeper:8181/catalog
quiet "${compose[@]}" run -d --rm --name "$runner" \
  -v "$state:/state" -p "127.0.0.1:$ui_port:$ui_port" \
  -e PYICEBERG_CATALOG__LAKEHOUSE__TYPE=rest \
  -e PYICEBERG_CATALOG__LAKEHOUSE__URI="$rest" \
  -e PYICEBERG_CATALOG__LAKEHOUSE__WAREHOUSE=demo \
  -e ICEBERG_REST_URI="$rest" -e ICEBERG_REST_WAREHOUSE=demo \
  -e DBT_DUCKDB_PATH=/state/nightly_shop.duckdb \
  -e DO_NOT_TRACK=1 -e DBT_SEND_ANONYMOUS_USAGE_STATS=false \
  runner sleep infinity
quiet docker exec -w /work "$runner" uv sync --frozen --inexact --extra mcp --extra ui --extra dbt
echo "   ready in $((SECONDS - begin))s"

story=(python3 "$root/docker/demo-e2e/story.py" --runner "$runner" --state "$state" --agent "$agent")
[[ -n "$screenshots" ]] && story+=(--screenshots "$screenshots")
# Run the story as a child we can stop: a signal sent to this script alone must not wait for it.
"${story[@]}" &
story_pid=$!
stop_story() {
  kill -TERM "$story_pid" 2>/dev/null || true
  wait "$story_pid" 2>/dev/null || true
  exit 130
}
trap stop_story INT TERM
wait "$story_pid"
