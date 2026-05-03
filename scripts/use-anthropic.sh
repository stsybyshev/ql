#!/usr/bin/env bash
# Switch OpenClaw back to Anthropic models (Sonnet + Haiku) and restart.
set -euo pipefail

CONFIG="$HOME/.openclaw/openclaw.json"

jq '
  .agents.defaults.model.primary = "anthropic/claude-sonnet-4-6" |
  .agents.defaults.subagents.model = "anthropic/claude-haiku-4-5"
' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"

echo "Switched to: Sonnet (main) + Haiku (subagents)"
openclaw gateway restart
