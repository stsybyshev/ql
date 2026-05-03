#!/usr/bin/env bash
# Regenerate and sync dashboard only when the MCP server has written new data.
# Called every minute by cron. Exits immediately if no dirty flag is present.
set -euo pipefail

DIRTY="/home/stan/.openclaw/workspace/food-tracker/.dashboard_dirty"
LOCK="/tmp/ql-nutrition-regen.lock"

[ -f "$DIRTY" ] || exit 0

exec 9>"$LOCK"
flock -n 9 || exit 0  # another run still in progress, skip

rm -f "$DIRTY"
cd /home/stan/dev/ql/skills/nutrition-tracker
/home/stan/.local/bin/uv run --with pyyaml -- python3 dashboard/generate.py \
  --config /home/stan/dev/ql/portal/assets/config.yaml
bash /home/stan/dev/ql/scripts/sync.sh
