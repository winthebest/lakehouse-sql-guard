#!/bin/sh
# Bootstrap Lakekeeper and create one warehouse whose storage profile hands out STS credentials
# for the silo bucket. Idempotent: re-running against an initialised catalog is a no-op.
# Runs inside the compose network (curlimages/curl); all values come from the environment.
set -eu

url="${LAKEKEEPER_URL:?}"
name="${WAREHOUSE_NAME:?}"

post() { # post PATH JSON -> prints the HTTP status, body in /tmp/body
  curl -sS -o /tmp/body -w '%{http_code}' -X POST "$url$1" \
    -H 'Content-Type: application/json' -d "$2"
}

code=$(post /management/v1/bootstrap '{"accept-terms-of-use": true}')
case "$code" in
  2??) echo "lakekeeper bootstrapped" ;;
  # An already bootstrapped server answers with a client error; verify it is that, not a typo.
  4??) grep -qi 'bootstrap' /tmp/body && echo "lakekeeper was already bootstrapped" \
         || { echo "bootstrap failed ($code): $(cat /tmp/body)" >&2; exit 1; } ;;
  *) echo "bootstrap failed ($code): $(cat /tmp/body)" >&2; exit 1 ;;
esac

payload=$(cat <<JSON
{
  "warehouse-name": "$name",
  "storage-profile": {
    "type": "s3",
    "bucket": "${S3_BUCKET:?}",
    "key-prefix": "wh",
    "endpoint": "${S3_ENDPOINT:?}",
    "sts-endpoint": "$S3_ENDPOINT",
    "region": "local-01",
    "path-style-access": true,
    "flavor": "s3-compat",
    "sts-enabled": true
  },
  "storage-credential": {
    "type": "s3",
    "credential-type": "access-key",
    "access-key-id": "${S3_ACCESS_KEY:?}",
    "secret-access-key": "${S3_SECRET_KEY:?}"
  }
}
JSON
)
if curl -sS "$url/management/v1/warehouse" | grep -q "\"name\": *\"$name\""; then
  echo "warehouse $name already exists"
  exit 0
fi
code=$(post /management/v1/warehouse "$payload")
case "$code" in
  2??) echo "warehouse $name created" ;;
  *) echo "warehouse creation failed ($code): $(cat /tmp/body)" >&2; exit 1 ;;
esac
