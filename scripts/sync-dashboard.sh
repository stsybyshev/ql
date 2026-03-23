#!/usr/bin/env bash
# Push dashboard.html to Google Drive via rclone.
#
# Usage:
#   ./sync-dashboard.sh                              # sync default file
#   ./sync-dashboard.sh --file /path/to/dashboard.html
#   ./sync-dashboard.sh --setup                      # one-time rclone config
#   ./sync-dashboard.sh --dry-run                    # show what would be synced
#
# Prerequisites:
#   sudo apt install rclone   (or: curl https://rclone.org/install.sh | sudo bash)
#   rclone config             (one-time: create remote named "gdrive", scope: drive.file)

set -euo pipefail

REMOTE="${RCLONE_REMOTE:-gdrive}"
DRIVE_FOLDER="${DRIVE_FOLDER:-Quantified Self}"
DASHBOARD="${1:-./dashboard.html}"

GREEN="\033[0;32m"
RED="\033[0;31m"
CYAN="\033[0;36m"
NC="\033[0m"

info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
err()   { echo -e "${RED}[err]${NC}   $*" >&2; }

# Parse flags
DRY_RUN=false
SETUP=false
FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --setup)    SETUP=true; shift ;;
        --dry-run)  DRY_RUN=true; shift ;;
        --file)     FILE="$2"; shift 2 ;;
        *)          shift ;;
    esac
done

[ -n "$FILE" ] && DASHBOARD="$FILE"

# Setup mode
if $SETUP; then
    if ! command -v rclone &>/dev/null; then
        err "rclone not found. Install with: sudo apt install rclone"
        exit 1
    fi
    info "Starting rclone config..."
    info "Create a remote named '${REMOTE}' with type 'drive' and scope 'drive.file'"
    rclone config
    exit 0
fi

# Pre-flight
if ! command -v rclone &>/dev/null; then
    err "rclone not found. Install with: sudo apt install rclone"
    err "Then run: $0 --setup"
    exit 1
fi

if ! rclone listremotes 2>/dev/null | grep -q "^${REMOTE}:"; then
    err "rclone remote '${REMOTE}' not configured."
    err "Run: $0 --setup"
    exit 1
fi

if [ ! -f "$DASHBOARD" ]; then
    err "Dashboard file not found: $DASHBOARD"
    err "Generate it first: python3 scripts/generate-dashboard.py"
    exit 1
fi

# Sync
info "Syncing to Google Drive..."
info "  File:   ${DASHBOARD}"
info "  Remote: ${REMOTE}:${DRIVE_FOLDER}/"

if $DRY_RUN; then
    rclone copy "$DASHBOARD" "${REMOTE}:${DRIVE_FOLDER}/" --dry-run
    info "Dry run — no changes made."
else
    rclone copy "$DASHBOARD" "${REMOTE}:${DRIVE_FOLDER}/" --progress
    ok "Synced to ${REMOTE}:${DRIVE_FOLDER}/$(basename "$DASHBOARD")"
    echo ""
    info "View in Google Drive: https://drive.google.com"
fi
