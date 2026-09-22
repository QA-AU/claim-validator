#!/usr/bin/env bash
# Azure auto-restarts a stopped PostgreSQL Flexible Server after 7 days —
# there's no way to keep it stopped indefinitely from the Azure side (see
# infra/README.md's cost-management notes). This re-stops it whenever it
# comes back, so the ~$19/month compute cost doesn't quietly resume between
# demos. Meant to run on a recurring local schedule (see the launchd job
# in this same directory) rather than by hand.
set -euo pipefail

RESOURCE_GROUP="claim-validator-rg"
SERVER_NAME="cv-pg-xkrgwrmtdxxuk"
LOG_FILE="$HOME/Library/Logs/claim-validator-pg-stop.log"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG_FILE"
}

state=$(az postgres flexible-server show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$SERVER_NAME" \
  --query state -o tsv 2>>"$LOG_FILE") || {
  log "ERROR: could not read server state (az login likely expired — run 'az login' again)"
  exit 1
}

if [ "$state" = "Ready" ]; then
  log "Server is Ready (auto-restarted by Azure) — stopping it."
  az postgres flexible-server stop \
    --resource-group "$RESOURCE_GROUP" \
    --name "$SERVER_NAME" >>"$LOG_FILE" 2>&1
  log "Stop command issued."
else
  log "Server already in state '$state' — nothing to do."
fi
