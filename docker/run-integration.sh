#!/usr/bin/env bash
# One command for the integration tests: start the stack of docker/compose.lakehouse.yml, run the
# tests in the `runner` container (the only place the catalogs' storage host name resolves), and
# remove the stack's containers, networks and volumes again, pass or fail. Needs only Docker.
#
#   docker/run-integration.sh                      all tests, Polaris included
#   docker/run-integration.sh -k trino -x          extra arguments go to pytest
#   SQLGUARD_IT_POLARIS=0 docker/run-integration.sh   skip Polaris (saves ~0.5 GB of memory)
#   SQLGUARD_KEEP_STACK=1 docker/run-integration.sh   leave the stack up for debugging; remove it with
#     docker compose -p sqlguard-lakehouse -f docker/compose.lakehouse.yml --profile '*' down -v
#
# It runs tests/integration (Lakekeeper, REST fixture, Polaris, Trino, Spark, composite) and the
# Docker tests of tests/safety/test_spark_adapter.py (`-m spark`). The compose project is
# `sqlguard-lakehouse`; nothing else on the machine is touched. Credentials are fixed demo values.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(docker compose -p sqlguard-lakehouse -f "$root/docker/compose.lakehouse.yml")

cleanup() {
  if [[ "${SQLGUARD_KEEP_STACK:-0}" != "1" ]]; then
    "${compose[@]}" --profile '*' down -v --remove-orphans
  fi
}
trap cleanup EXIT

# Only long-running services are waited for; Lakekeeper's bootstrap is a dependency of Trino and
# Spark, and Polaris' runs below (a finished one-shot makes `up --wait` fail).
selection=()
use_polaris=0
[[ "${SQLGUARD_IT_POLARIS:-1}" == "1" ]] && use_polaris=1

"${compose[@]}" build runner
if [[ "$use_polaris" == "1" ]]; then
  "${compose[@]}" --profile polaris up -d --wait rest-fixture trino spark polaris
  "${compose[@]}" --profile polaris run --rm polaris-bootstrap
else
  selection=(-k "not polaris")
  "${compose[@]}" up -d --wait rest-fixture trino spark
fi
"${compose[@]}" run --rm runner uv run --frozen --extra spark --extra mcp \
  pytest -p no:cacheprovider -m "integration or spark" \
  tests/integration tests/safety/test_spark_adapter.py ${selection[@]+"${selection[@]}"} "$@"
