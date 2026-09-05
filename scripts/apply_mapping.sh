#!/usr/bin/env sh
# Create the target index with the recommended mapping (Elasticsearch 8/9 format).
# Equivalent to `evtxtoelk ... --create-index`; kept for people who script curl.
#
#   ES_URL=http://localhost:9200 INDEX=hostlogs ./scripts/apply_mapping.sh
set -eu
ES_URL="${ES_URL:-http://localhost:9200}"
INDEX="${INDEX:-hostlogs}"
curl -sS -X PUT "$ES_URL/$INDEX" -H 'Content-Type: application/json' -d '{
  "mappings": {
    "date_detection": false,
    "numeric_detection": false,
    "properties": {
      "@timestamp": { "type": "date" },
      "Event": {
        "properties": {
          "System": {
            "properties": {
              "TimeCreated": {
                "properties": { "@SystemTime": { "type": "date" } }
              }
            }
          }
        }
      }
    }
  }
}'
echo
