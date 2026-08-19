#!/usr/bin/env bash
# Install nutrition-clerk as a systemd --user service.
#
# Idempotent: safe to re-run after editing the unit or upgrading the bot.
# Run from your own login shell (not via a tool/agent) — `systemctl --user`
# needs the session D-Bus, which non-login shells do not have.
set -euo pipefail

PROJECT_DIR="/home/stan/dev/ql/bots/nutrition-clerk"
UNIT_NAME="nutrition-clerk.service"
UNIT_SRC="${PROJECT_DIR}/deploy/${UNIT_NAME}"
UNIT_DEST="${HOME}/.config/systemd/user/${UNIT_NAME}"
CONFIG="${HOME}/.config/nutrition-clerk/config.toml"
ENV_FILE="${HOME}/.config/nutrition-clerk/env"

say() { printf '  %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

echo "==> Preflight"

[ -f "$UNIT_SRC" ] || fail "unit file not found: $UNIT_SRC"

if ! systemctl --user is-system-running >/dev/null 2>&1; then
  fail "cannot reach the systemd user manager.
  Run this from your own login shell. If you are already there, WSL may need
  systemd enabled: add 'systemd=true' under [boot] in /etc/wsl.conf, then
  'wsl --shutdown' from Windows."
fi
say "systemd user manager: reachable"

[ -f "$CONFIG" ] || fail "config not found: $CONFIG
  cp ${PROJECT_DIR}/config/config.example.toml $CONFIG && chmod 600 $CONFIG"
say "config: $CONFIG"

# Refuse to install while a HAND-STARTED bot is polling: Telegram permits ONE
# getUpdates consumer per token, so two instances fight with 409 Conflict and
# messages land unpredictably.
#
# Anchored to the END of the command line, which is what distinguishes the bot
# itself from everything else living under a directory called
# "nutrition-clerk". It matches `uv run nutrition-clerk`, the unit's
# `uv run --project <dir> nutrition-clerk`, and the console script it execs
# (<venv>/bin/nutrition-clerk) — but NOT the MCP subprocess (ends in
# server.py) and not this script (ends in install.sh).
#
# Two earlier versions of this check were wrong: "nutrition_clerk.main" (how
# the bot is NEVER started, so a stray instance slipped through) and an
# unanchored ".*nutrition-clerk" (which swept up the MCP subprocess and any
# shell whose command line merely mentioned the path).
_BOT_PROCS='nutrition-clerk$'

if systemctl --user is-active --quiet "$UNIT_NAME"; then
  say "service already running — it will be restarted"
elif pgrep -f "$_BOT_PROCS" >/dev/null 2>&1; then
  fail "a hand-started nutrition-clerk is already polling (pid $(pgrep -f "$_BOT_PROCS" | tr '\n' ' ')).
  Stop it before installing the service, or two pollers will compete for the
  same Telegram token:
      pkill -f '$_BOT_PROCS'"
else
  say "no competing instance running"
fi

echo
echo "==> API key"
if [ ! -f "$ENV_FILE" ]; then
  mkdir -p "$(dirname "$ENV_FILE")"
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    printf 'ANTHROPIC_API_KEY=%s\n' "$ANTHROPIC_API_KEY" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    say "wrote $ENV_FILE from the current shell's ANTHROPIC_API_KEY (mode 600)"
  else
    printf 'ANTHROPIC_API_KEY=\n' > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    say "created $ENV_FILE — add your key to it before starting"
  fi
else
  chmod 600 "$ENV_FILE"
  if grep -q '^ANTHROPIC_API_KEY=.\+' "$ENV_FILE"; then
    say "$ENV_FILE already has a key (mode 600)"
  else
    say "WARNING: $ENV_FILE exists but ANTHROPIC_API_KEY looks empty"
  fi
fi

echo
echo "==> Install unit"
mkdir -p "$(dirname "$UNIT_DEST")"
install -m 644 "$UNIT_SRC" "$UNIT_DEST"
say "installed $UNIT_DEST"

systemctl --user daemon-reload
say "daemon-reload done"

# Survive logout / keep running when no session is open.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  loginctl enable-linger "$USER" && say "enabled linger for $USER"
else
  say "linger already enabled"
fi

systemctl --user enable "$UNIT_NAME" >/dev/null
say "enabled at boot"

systemctl --user restart "$UNIT_NAME"
say "started"

echo
echo "==> Status"
sleep 3
systemctl --user --no-pager --lines=0 status "$UNIT_NAME" || true

cat <<EOF

Done. Useful commands:

  systemctl --user status  nutrition-clerk
  systemctl --user restart nutrition-clerk
  systemctl --user stop    nutrition-clerk
  journalctl --user -u nutrition-clerk -f          # live log
  journalctl --user -u nutrition-clerk --since today

Per-turn detail (prompts, model responses, timings):
  jq 'select(.error)' ~/.local/state/nutrition-clerk/turns.jsonl
EOF
