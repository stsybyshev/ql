#!/usr/bin/env bash
# Install/uninstall a cron job that updates monthly food log summaries.
#
# Usage:
#   ./install-cron.sh                        # install with default schedule (midnight daily)
#   ./install-cron.sh --schedule "0 */6 * * *"  # every 6 hours
#   ./install-cron.sh --dry-run              # show what would be installed
#   ./install-cron.sh --status               # check if cron job is installed
#   ./install-cron.sh --uninstall            # remove the cron job

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SUMMARY_SCRIPT="${SCRIPT_DIR}/update-monthly-summary.py"
BACKUP_SCRIPT="${SCRIPT_DIR}/backup-foodlogs.sh"

OPENCLAW_HOME="${OPENCLAW_HOME:-${HOME}/.openclaw}"
WORKSPACE="${OPENCLAW_HOME}/workspace"
FOOD_LOG_DIR="${WORKSPACE}/food-tracker"

CRON_MARKER="# openclaw-food-tracker-summary"
BACKUP_MARKER="# openclaw-food-tracker-backup"
DEFAULT_SCHEDULE="0 0 * * *"
BACKUP_SCHEDULE="55 23 * * *"

# ── Colours ───────────────────────────────────────────────────────────
GREEN="\033[0;32m"
RED="\033[0;31m"
YELLOW="\033[1;33m"
CYAN="\033[0;36m"
NC="\033[0m"

info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()   { echo -e "${RED}[err]${NC}   $*" >&2; }

# ── Parse flags ───────────────────────────────────────────────────────
DRY_RUN=false
UNINSTALL=false
STATUS=false
SCHEDULE="$DEFAULT_SCHEDULE"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true; shift ;;
        --uninstall)  UNINSTALL=true; shift ;;
        --status)     STATUS=true; shift ;;
        --schedule)   SCHEDULE="$2"; shift 2 ;;
        *)            err "Unknown flag: $1"; exit 1 ;;
    esac
done

# ── Status ────────────────────────────────────────────────────────────
if $STATUS; then
    FOUND=false
    if crontab -l 2>/dev/null | grep -q "$BACKUP_MARKER"; then
        ok "Backup job is installed:"
        crontab -l 2>/dev/null | grep "$BACKUP_MARKER"
        FOUND=true
    fi
    if crontab -l 2>/dev/null | grep -q "$CRON_MARKER"; then
        ok "Summary job is installed:"
        crontab -l 2>/dev/null | grep "$CRON_MARKER"
        FOUND=true
    fi
    if ! $FOUND; then
        info "No cron jobs installed."
    fi
    exit 0
fi

# ── Uninstall ─────────────────────────────────────────────────────────
if $UNINSTALL; then
    FOUND=false
    crontab -l 2>/dev/null | grep -q "$CRON_MARKER" && FOUND=true
    crontab -l 2>/dev/null | grep -q "$BACKUP_MARKER" && FOUND=true
    if ! $FOUND; then
        warn "No cron jobs found to remove."
        exit 0
    fi
    if $DRY_RUN; then
        info "Would remove cron entries:"
        crontab -l 2>/dev/null | grep -E "$CRON_MARKER|$BACKUP_MARKER"
    else
        crontab -l 2>/dev/null | grep -v "$CRON_MARKER" | grep -v "$BACKUP_MARKER" | crontab -
        ok "Cron jobs removed (summary + backup)."
    fi
    exit 0
fi

# ── Pre-flight checks ────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    err "python3 not found. Please install Python 3."
    exit 1
fi

if [ ! -f "$SUMMARY_SCRIPT" ]; then
    err "Summary script not found: $SUMMARY_SCRIPT"
    exit 1
fi

if [ ! -d "$WORKSPACE" ]; then
    err "OpenClaw workspace not found: $WORKSPACE"
    err "Is OpenClaw installed? Set OPENCLAW_HOME to override."
    exit 1
fi

if [ ! -d "$FOOD_LOG_DIR" ]; then
    info "Food log directory does not exist yet: $FOOD_LOG_DIR"
    if $DRY_RUN; then
        info "Would create: $FOOD_LOG_DIR"
    else
        mkdir -p "$FOOD_LOG_DIR"
        ok "Created: $FOOD_LOG_DIR"
    fi
fi

# ── Build cron lines ─────────────────────────────────────────────────
PYTHON3_PATH="$(command -v python3)"
BASH_PATH="$(command -v bash)"
CRON_LINE="${SCHEDULE} ${PYTHON3_PATH} ${SUMMARY_SCRIPT} --data-dir ${FOOD_LOG_DIR} --current-month ${CRON_MARKER}"
BACKUP_LINE="${BACKUP_SCHEDULE} ${BASH_PATH} ${BACKUP_SCRIPT} --data-dir ${FOOD_LOG_DIR} ${BACKUP_MARKER}"

# ── Install ──────────────────────────────────────────────────────────
echo ""
info "Cron job configuration:"
info ""
info "  1) Backup (runs first at 23:55)"
info "     ${BACKUP_LINE}"
info ""
info "  2) Summary recalc (runs at midnight)"
info "     ${CRON_LINE}"
echo ""

if $DRY_RUN; then
    info "Dry run — no changes made."
    exit 0
fi

# Remove existing entries (if any) and add new ones
EXISTING_CRONTAB=$(crontab -l 2>/dev/null || true)
NEW_CRONTAB=$(echo "$EXISTING_CRONTAB" | grep -v "$CRON_MARKER" | grep -v "$BACKUP_MARKER" || true)

if [ -n "$NEW_CRONTAB" ]; then
    printf '%s\n%s\n%s\n' "$NEW_CRONTAB" "$BACKUP_LINE" "$CRON_LINE" | crontab -
else
    printf '%s\n%s\n' "$BACKUP_LINE" "$CRON_LINE" | crontab -
fi

ok "Cron jobs installed (backup at 23:55 + summary at midnight)."
echo ""
info "Verify with: crontab -l | grep openclaw"
info "Remove with: $0 --uninstall"
echo ""
