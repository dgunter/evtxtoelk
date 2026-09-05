#!/usr/bin/env sh
# Delete the target index.
#
#   ES_URL=http://localhost:9200 INDEX=hostlogs ./scripts/clear_index.sh
set -eu
ES_URL="${ES_URL:-http://localhost:9200}"
INDEX="${INDEX:-hostlogs}"
curl -sS -X DELETE "$ES_URL/$INDEX"
echo
