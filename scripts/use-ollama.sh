#!/usr/bin/env bash
# Switch OpenClaw to local Ollama model (Gemma 4 E4B) and restart.
# Usage: bash scripts/use-ollama.sh [model]
#   Default model: gemma4:e4b
#   Alternative:   VladimirGav/gemma4-26b-16GB-VRAM
set -euo pipefail

MODEL="${1:-gemma4:e4b}"
CONFIG="$HOME/.openclaw/openclaw.json"

# Verify the model is available locally before switching
if ! ollama list | grep -q "$(echo "$MODEL" | cut -d: -f1)"; then
  echo "ERROR: Model '$MODEL' not found in Ollama. Run: ollama pull $MODEL"
  exit 1
fi

jq --arg m "ollama/$MODEL" '
  .agents.defaults.model.primary = $m |
  .agents.defaults.subagents.model = $m
' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"

echo "Switched to: ollama/$MODEL (main + subagents)"
openclaw gateway restart
