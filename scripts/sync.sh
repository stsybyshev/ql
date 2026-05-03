#!/usr/bin/env bash
# Quantified Life — Cloud sync via rclone
# Reads sync.yaml for provider config and file list.
#
# Usage:
#   bash scripts/sync.sh              # sync files
#   bash scripts/sync.sh --dry-run    # preview what would be synced
#   bash scripts/sync.sh --status     # check rclone remote status
#   bash scripts/sync.sh --setup      # configure rclone remote

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$SCRIPT_DIR/sync.yaml"

# Parse sync.yaml (simple grep — avoids pyyaml dependency in bash)
REMOTE=$(grep '^remote:' "$CONFIG" | awk '{print $2}')
REMOTE_PATH=$(grep '^remote_path:' "$CONFIG" | sed 's/^remote_path: *//;s/"//g')

if ! command -v rclone &>/dev/null; then
    echo "ERROR: rclone not installed. Install with: curl https://rclone.org/install.sh | sudo bash"
    exit 1
fi

case "${1:-sync}" in
    --setup)
        echo "Configuring rclone remote '$REMOTE'..."
        rclone config
        ;;
    --status)
        echo "Checking remote '$REMOTE:$REMOTE_PATH'..."
        rclone lsd "$REMOTE:$REMOTE_PATH" 2>/dev/null && echo "Remote accessible." || echo "Remote not configured or inaccessible."
        ;;
    --dry-run)
        echo "Dry run — would sync these files to $REMOTE:$REMOTE_PATH/"
        grep '^ *-' "$CONFIG" | sed 's/^ *- *//' | while read -r file; do
            src=$(eval echo "$file")  # expand ~ if present
            [[ "$src" != /* ]] && src="$REPO_DIR/$src"
            if [ -f "$src" ]; then
                echo "  $src → $REMOTE:$REMOTE_PATH/$(basename "$src")"
            else
                echo "  SKIP (not found): $src"
            fi
        done
        ;;
    sync|"")
        grep '^ *-' "$CONFIG" | sed 's/^ *- *//' | while read -r file; do
            src=$(eval echo "$file")  # expand ~ if present
            [[ "$src" != /* ]] && src="$REPO_DIR/$src"
            if [ -f "$src" ]; then
                dest="$REMOTE:$REMOTE_PATH/$(basename "$src")"
                echo "Syncing $src → $dest"
                rclone copyto --checksum "$src" "$dest"
            else
                echo "SKIP (not found): $src"
            fi
        done
        echo "Sync complete."
        ;;
    *)
        echo "Usage: sync.sh [--dry-run|--status|--setup]"
        exit 1
        ;;
esac
