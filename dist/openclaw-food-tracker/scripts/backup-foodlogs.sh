#!/usr/bin/env bash
# Back up all YYYY-MM.md food log files into a timestamped tar.gz archive.
#
# Usage:
#   ./backup-foodlogs.sh                          # backup with defaults
#   ./backup-foodlogs.sh --data-dir /path/to/logs
#   ./backup-foodlogs.sh --backup-dir /path/to/backups
#   ./backup-foodlogs.sh --dry-run
#
# Keeps the last N backups (default 30). Older ones are pruned automatically.

set -euo pipefail

OPENCLAW_HOME="${OPENCLAW_HOME:-${HOME}/.openclaw}"
DATA_DIR="${OPENCLAW_HOME}/workspace/food-tracker"
BACKUP_DIR="${HOME}/openclaw_backups/food-tracker"
KEEP=30
DRY_RUN=false

GREEN="\033[0;32m"
RED="\033[0;31m"
CYAN="\033[0;36m"
NC="\033[0m"

info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
err()   { echo -e "${RED}[err]${NC}   $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-dir)    DATA_DIR="$2"; shift 2 ;;
        --backup-dir)  BACKUP_DIR="$2"; shift 2 ;;
        --keep)        KEEP="$2"; shift 2 ;;
        --dry-run)     DRY_RUN=true; shift ;;
        *)             err "Unknown flag: $1"; exit 1 ;;
    esac
done

# Pre-flight
if [ ! -d "$DATA_DIR" ]; then
    err "Data directory not found: $DATA_DIR"
    exit 1
fi

FILES=$(find "$DATA_DIR" -maxdepth 1 -name "????-??.md" -type f | sort)
COUNT=$(echo "$FILES" | grep -c . || true)

if [ "$COUNT" -eq 0 ]; then
    info "No YYYY-MM.md files found in $DATA_DIR — nothing to back up."
    exit 0
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
ARCHIVE_NAME="food-logs-${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_DIR}/${ARCHIVE_NAME}"

info "Backing up ${COUNT} food log file(s)"
info "  Source:  ${DATA_DIR}"
info "  Target:  ${ARCHIVE_PATH}"

if $DRY_RUN; then
    echo "$FILES" | while read -r f; do info "  $(basename "$f")"; done
    info "Dry run — no changes made."
    exit 0
fi

mkdir -p "$BACKUP_DIR"

# Create tar.gz from the data directory so paths inside are just filenames
(cd "$DATA_DIR" && find . -maxdepth 1 -name "????-??.md" -print0 | tar -czf "$ARCHIVE_PATH" --null -T -)

SIZE=$(du -h "$ARCHIVE_PATH" | cut -f1)
ok "Created ${ARCHIVE_NAME} (${SIZE}, ${COUNT} files)"

# Prune old backups beyond --keep limit
EXISTING=$(find "$BACKUP_DIR" -maxdepth 1 -name "food-logs-*.tar.gz" -type f | sort)
TOTAL=$(echo "$EXISTING" | grep -c . || true)

if [ "$TOTAL" -gt "$KEEP" ]; then
    TO_DELETE=$((TOTAL - KEEP))
    echo "$EXISTING" | head -n "$TO_DELETE" | while read -r old; do
        rm "$old"
        info "Pruned old backup: $(basename "$old")"
    done
    ok "Kept last ${KEEP} backups, pruned ${TO_DELETE}"
fi
