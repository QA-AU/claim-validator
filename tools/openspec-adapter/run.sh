#!/usr/bin/env bash
# Run the OpenSpec -> claim-validator adapter against an OpenSpec project
# that lives in a SEPARATE folder from this repo.
#
# Usage:
#   tools/openspec-adapter/run.sh <path> [out-dir]
#
#   <path>     one of:
#                - an OpenSpec project root  (has an openspec/ directory)
#                - a specs directory         (has <capability>/spec.md)
#                - a single change's specs/  directory
#                - a single spec.md file
#   [out-dir]  where claims.json / claims.map.json are written
#              (default: ./openspec-claims)
#
# When given a project root, this points at openspec/changes/ (the "validate
# this proposal" case). To validate the living specs instead, pass
# <path>/openspec/specs explicitly. Archived changes are skipped.
#
# This script does NOT call claim-validator. It stops at claims.json; the
# last lines it prints are the API calls you run next with your own client.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER="$HERE/openspec_to_claims.py"

SRC="${1:?usage: run.sh <path-to-openspec-project-or-specs-dir> [out-dir]}"
OUT="${2:-./openspec-claims}"

if [ -f "$SRC" ]; then
  TARGET="$SRC"
elif [ -d "$SRC/openspec/changes" ]; then
  TARGET="$SRC/openspec/changes"
  echo "note: using $TARGET (change proposals). For living specs, pass $SRC/openspec/specs" >&2
elif [ -d "$SRC/openspec/specs" ]; then
  TARGET="$SRC/openspec/specs"
elif [ -d "$SRC" ]; then
  TARGET="$SRC"
else
  echo "error: no such file or directory: $SRC" >&2
  exit 1
fi

mkdir -p "$OUT"
python3 "$ADAPTER" "$TARGET" \
  --map "$OUT/claims.map.json" \
  -o "$OUT/claims.json" \
  --stats

cat >&2 <<EOF

wrote:
  $OUT/claims.json      -> the "claims" array for POST /api/validations
  $OUT/claims.map.json  -> claim id -> source requirement / scenario / bullet

next (with your own HTTP client, against the SOURCE DOCUMENT these specs
were written from — for a delta spec, also upload the pre-change spec):
  POST /api/documents     # upload the source brief; note each returned "path"
  POST /api/ontologies    # { "files": [<those paths>] }
  POST /api/validations    # { "ontology_key": "<key>", "claims": <claims.json> }
  GET  /api/validations/{job_id}          # poll until "done"
  GET  /api/validations/{job_id}/report   # download the .xlsx
EOF
