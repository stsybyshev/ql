#!/usr/bin/env bash
# Switch OpenClaw back to Anthropic models (Sonnet + Haiku) and restart.
set -euo pipefail

CONFIG="$HOME/.openclaw/openclaw.json"

jq '
  .agents.defaults.model.primary = "anthropic/claude-sonnet-4-6" |
  .agents.defaults.subagents.model = "anthropic/claude-haiku-4-5"
' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"

# NOTE: Reverted to Sonnet 4.6 on 2026-07-02 because OpenClaw 2026.4.5 does not
# yet recognise "anthropic/claude-sonnet-5" (falls back to primary). Bump to
# claude-sonnet-5 once `npm view openclaw version` bumps AND its changelog
# lists Sonnet 5 support, then run: npm i -g openclaw@latest.
echo "Switched to: Sonnet 4.6 (main) + Haiku 4.5 (subagents)"
openclaw gateway restart
