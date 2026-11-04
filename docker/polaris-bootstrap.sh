#!/bin/sh
# Create the Polaris catalog the tests use. The storage config sets stsUnavailable (the silo STS
# is not an AWS STS), so Polaris vends no credentials and clients use static ones.
# Idempotent. Runs inside the compose network (curlimages/curl).
set -eu

url="${POLARIS_URL:?}"
catalog="${POLARIS_CATALOG:?}"

token=$(curl -sS -X POST "$url/api/catalog/v1/oauth/tokens" \
  -H 'Polaris-Realm: POLARIS' \
  -d "grant_type=client_credentials&client_id=${POLARIS_CLIENT_ID:?}&client_secret=${POLARIS_CLIENT_SECRET:?}&scope=PRINCIPAL_ROLE:ALL" \
  | sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p')
[ -n "$token" ] || { echo "could not get a Polaris token" >&2; exit 1; }

payload=$(cat <<JSON
{
  "catalog": {
    "name": "$catalog",
    "type": "INTERNAL",
    "readOnly": false,
    "properties": {"default-base-location": "${S3_BASE_LOCATION:?}"},
    "storageConfigInfo": {
      "storageType": "S3",
      "allowedLocations": ["$S3_BASE_LOCATION"],
      "endpoint": "${S3_ENDPOINT:?}",
      "endpointInternal": "$S3_ENDPOINT",
      "pathStyleAccess": true,
      "stsUnavailable": true,
      "region": "us-west-2"
    }
  }
}
JSON
)
code=$(curl -sS -o /tmp/body -w '%{http_code}' -X POST "$url/api/management/v1/catalogs" \
  -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -H 'Polaris-Realm: POLARIS' -d "$payload")
case "$code" in
  2??) echo "polaris catalog $catalog created" ;;
  409) echo "polaris catalog $catalog already exists" ;;
  *) echo "polaris catalog creation failed ($code): $(cat /tmp/body)" >&2; exit 1 ;;
esac
