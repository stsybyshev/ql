#!/usr/bin/env bash
# Deploy dashboard to OpenClaw workspace (PROD).
# Run this after editing generate.py, config.yaml, or the HTML template.
#
# Usage:
#   bash scripts/deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE="$HOME/.openclaw/workspace/food-tracker"

mkdir -p "$WORKSPACE/dashboard" "$WORKSPACE/scripts"

# Python pipeline
cp "$REPO_DIR/skills/nutrition-tracker/dashboard/generate.py"   "$WORKSPACE/dashboard/generate.py"
cp "$REPO_DIR/skills/nutrition-tracker/scripts/parse_foodlog.py" "$WORKSPACE/scripts/parse_foodlog.py"

# Config and HTML template
cp "$REPO_DIR/portal/assets/config.yaml"           "$WORKSPACE/dashboard/config.yaml"
cp "$REPO_DIR/portal/assets/quantified-life.html"  "$WORKSPACE/dashboard/template.html"

# Workspace-specific shell scripts (paths baked in)
cat > "$WORKSPACE/scripts/sync.sh" <<'EOF'
#!/usr/bin/env bash
# Sync dashboard HTML to Google Drive.
set -euo pipefail
WORKSPACE="$HOME/.openclaw/workspace/food-tracker"
rclone copyto --checksum \
  "$WORKSPACE/dashboard/quantified-life.html" \
  "gdrive:Quantified Self/quantified-life.html"
echo "Sync complete."
EOF

cat > "$WORKSPACE/scripts/regen-if-dirty.sh" <<'EOF'
#!/usr/bin/env bash
# Regenerate dashboard if the MCP server has written new data.
set -euo pipefail
WORKSPACE="$HOME/.openclaw/workspace/food-tracker"
DIRTY="$WORKSPACE/.dashboard_dirty"
LOCK="/tmp/ql-nutrition-regen.lock"

[ -f "$DIRTY" ] || exit 0

exec 9>"$LOCK"
flock -n 9 || exit 0  # another run in progress, skip

rm -f "$DIRTY"
/home/stan/.local/bin/uv run --with pyyaml python3 \
  "$WORKSPACE/dashboard/generate.py" \
  --config   "$WORKSPACE/dashboard/config.yaml" \
  --template "$WORKSPACE/dashboard/template.html"
bash "$WORKSPACE/scripts/sync.sh"
EOF

chmod +x "$WORKSPACE/scripts/sync.sh" "$WORKSPACE/scripts/regen-if-dirty.sh"

echo "Deployed to $WORKSPACE"
echo "  dashboard/generate.py"
echo "  dashboard/config.yaml"
echo "  dashboard/template.html"
echo "  scripts/parse_foodlog.py"
echo "  scripts/sync.sh"
echo "  scripts/regen-if-dirty.sh"
