#!/usr/bin/env bash
# Deploy openclaw-food-tracker skill to local OpenClaw installation.
#
# Usage:
#   ./deploy.sh              # deploy (copy) to ~/.openclaw/workspace/skills/
#   ./deploy.sh --dry-run    # show what would be copied without doing it
#   ./deploy.sh --symlink    # symlink instead of copy (for development)
#   ./deploy.sh --uninstall  # remove the installed skill

set -euo pipefail

SKILL_NAME="openclaw-food-tracker"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)/dist/${SKILL_NAME}"
DEST_BASE="${OPENCLAW_SKILLS_DIR:-${HOME}/.openclaw/workspace/skills}"
DEST_DIR="${DEST_BASE}/${SKILL_NAME}"

# ── Colours ──────────────────────────────────────────────────────────
GREEN="\033[0;32m"
RED="\033[0;31m"
YELLOW="\033[1;33m"
CYAN="\033[0;36m"
NC="\033[0m"

# ── Helpers ──────────────────────────────────────────────────────────
info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()   { echo -e "${RED}[err]${NC}   $*" >&2; }

# ── Pre-flight checks ───────────────────────────────────────────────
if [ ! -d "$SRC_DIR" ]; then
    err "Source not found: ${SRC_DIR}"
    err "Run this script from the repository root."
    exit 1
fi

if [ ! -f "${SRC_DIR}/SKILL.md" ]; then
    err "No SKILL.md in ${SRC_DIR} — is the artifact built?"
    exit 1
fi

if [ ! -d "$DEST_BASE" ]; then
    err "OpenClaw skills directory not found: ${DEST_BASE}"
    err "Is OpenClaw installed? Set OPENCLAW_SKILLS_DIR to override."
    exit 1
fi

# ── Parse flags ──────────────────────────────────────────────────────
DRY_RUN=false
SYMLINK=false
UNINSTALL=false

for arg in "$@"; do
    case "$arg" in
        --dry-run)   DRY_RUN=true ;;
        --symlink)   SYMLINK=true ;;
        --uninstall) UNINSTALL=true ;;
        *)           err "Unknown flag: $arg"; exit 1 ;;
    esac
done

# ── Uninstall ────────────────────────────────────────────────────────
if $UNINSTALL; then
    if [ -e "$DEST_DIR" ]; then
        if $DRY_RUN; then
            info "Would remove: ${DEST_DIR}"
        else
            rm -rf "$DEST_DIR"
            ok "Removed ${DEST_DIR}"
            info "Restart OpenClaw to pick up the change."
        fi
    else
        warn "Nothing to uninstall — ${DEST_DIR} does not exist."
    fi
    exit 0
fi

# ── Deploy ───────────────────────────────────────────────────────────
file_count=$(find "$SRC_DIR" -type f | wc -l)

echo ""
info "Deploying ${SKILL_NAME} (${file_count} files)"
info "  From: ${SRC_DIR}"
info "  To:   ${DEST_DIR}"
info "  Mode: $( $SYMLINK && echo 'symlink' || echo 'copy' )"
echo ""

if $DRY_RUN; then
    info "Files that would be deployed:"
    find "$SRC_DIR" -type f -printf "    %P\n"
    echo ""
    info "Dry run — no changes made."
    exit 0
fi

if $SYMLINK; then
    # Remove existing (could be dir or symlink from previous deploy)
    [ -e "$DEST_DIR" ] && rm -rf "$DEST_DIR"
    ln -s "$SRC_DIR" "$DEST_DIR"
    ok "Symlinked ${DEST_DIR} -> ${SRC_DIR}"
else
    # Remove existing and copy fresh
    [ -e "$DEST_DIR" ] && rm -rf "$DEST_DIR"
    cp -r "$SRC_DIR" "$DEST_DIR"
    ok "Copied ${file_count} files to ${DEST_DIR}"
fi

# ── Seed workspace files ─────────────────────────────────────────────
WORKSPACE="${HOME}/.openclaw/workspace"
FOOD_DIR="${WORKSPACE}/food-tracker"
mkdir -p "$FOOD_DIR"

# Copy personal-foods.yaml to workspace if not already there
if [ ! -f "${FOOD_DIR}/personal-foods.yaml" ]; then
    cp "${DEST_DIR}/references/personal-foods.yaml" "${FOOD_DIR}/personal-foods.yaml"
    ok "Seeded ${FOOD_DIR}/personal-foods.yaml (first install)"
else
    info "Workspace personal-foods.yaml already exists — not overwriting"
fi

# ── Verify ───────────────────────────────────────────────────────────
echo ""
info "Installed files:"
find "$DEST_DIR" -type f -printf "    %P\n" 2>/dev/null || \
    find -L "$DEST_DIR" -type f | sed "s|${DEST_DIR}/|    |"

echo ""
ok "Deploy complete. Restart OpenClaw to pick up the skill."
echo ""
