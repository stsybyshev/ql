# nutrition-clerk

Telegram in, MCP-backed meal logging out. Runs alongside Veda (the
general-purpose OpenClaw agent) and takes over meal-entry work to cut
per-turn cost.

Architecture is "LLM at the seams, Python in the middle": deterministic
Python owns routing, dispatch, unit rules and formatting; the LLM is called
only to parse free text, read a label photo, or estimate an unknown food.
Typically **1 LLM call per turn** (2 when a photo is attached).

See `../../REFACTOR-BOT.MD` for the spec and
`~/.claude/plans/logical-swimming-duckling.md` for the milestone plan.

## Setup

```bash
cd /home/stan/dev/ql/bots/nutrition-clerk
uv sync --extra dev
```

### 1. Register the bot

See **Telegram bot setup** below.

### 2. Configure

```bash
mkdir -p ~/.config/nutrition-clerk
cp config/config.example.toml ~/.config/nutrition-clerk/config.toml
chmod 600 ~/.config/nutrition-clerk/config.toml
```

Fill in `bot.telegram_token` and `bot.allowed_chat_ids`, then set
`bot.channel = "telegram"`.

### 3. Seed the double-entry files

The shipped config keeps the clerk's output **separate** from Veda's so both
can run in parallel and be compared:

```bash
mkdir -p ~/.openclaw/workspace/food-tracker-clerk
cp ~/.openclaw/workspace/food-tracker/personal-foods.yaml \
   ~/.openclaw/workspace/food-tracker-clerk/personal-foods.yaml
```

`popular-foods.yaml` is shared — it is read-only seed data.

### 4. Run

```bash
ANTHROPIC_API_KEY=... uv run nutrition-clerk
```

## Run as a service (systemd --user)

```bash
# From YOUR login shell — systemctl --user needs the session D-Bus,
# which non-login shells (and agents) do not have.
cd /home/stan/dev/ql/bots/nutrition-clerk
./deploy/install.sh
```

The script is idempotent and refuses to install while a hand-started bot is
running — Telegram allows **one** `getUpdates` consumer per token, so two
instances fight with 409 Conflict and messages arrive unpredictably. Stop any
foreground bot first.

It writes `~/.config/nutrition-clerk/env` (mode 600) for `ANTHROPIC_API_KEY`,
picking the key up from your shell if it is exported. The key is deliberately
kept out of the unit file, which is world-readable and shows up in
`systemctl cat`.

```bash
systemctl --user status  nutrition-clerk
systemctl --user restart nutrition-clerk      # after a code change
systemctl --user stop    nutrition-clerk
journalctl --user -u nutrition-clerk -f       # live log
```

Linger is enabled so the bot keeps running when no session is open. On WSL,
systemd needs `systemd=true` under `[boot]` in `/etc/wsl.conf` (already the
case here, since the OpenClaw gateway runs the same way).

## Comparing against Veda

```bash
diff <(grep '^| [0-9]' ~/.openclaw/workspace/food-tracker/2026-08.md) \
     <(grep '^| [0-9]' ~/.openclaw/workspace/food-tracker-clerk/2026-08.md)
```

To cut over: comment out `food_log_dir` and `personal_foods_path` in
`[mcp.food_tracker]` so the clerk writes where Veda does, and stop sending
meals to Veda.

## Troubleshooting

Every turn appends a JSON record to `~/.local/state/nutrition-clerk/turns.jsonl`
with the inputs, each node (with timings), the **actual prompts and model
responses**, the reply, and a traceback if it failed.

```bash
jq 'select(.error)'                  ~/.local/state/nutrition-clerk/turns.jsonl
jq '{id:.turn_id, ms:.total_ms}'     ~/.local/state/nutrition-clerk/turns.jsonl
jq -r 'select(.turn_id=="t-abc123") | .nodes[] | "\(.node) \(.ms)ms"' \
                                     ~/.local/state/nutrition-clerk/turns.jsonl
```

Photos from failed turns are kept under
`~/.local/state/nutrition-clerk/failed-turns/<turn_id>/` so a crash can be
reproduced.

Knobs live in `[tracing]` — `record_payloads`, `max_payload_chars`.

## Test

```bash
uv run pytest tests/unit           # fast, no network
uv run pytest tests/integration    # live LLM; needs ANTHROPIC_API_KEY
```

## What it handles

| Message | Path |
|---|---|
| `1 apple` | cache lookup |
| `ssundubu-jigaye` (typo) | fuzzy cache fallback |
| `chia pudding 300 kcal 12P 20F 8C, save it` | typed macros + save |
| `200g of Manchego` + label photo | label OCR (per-100g, 100g rule) |
| `Thai dinner: jungle curry, jasmine rice` + meal photo | per-dish estimate |
| `1 banana` (not cached) | world-knowledge estimate |
| `save the pomegranate juice I had` | promote a recent row to cache |
| `this morning I had...` | natural-language timestamps |
| `200g cherries and 60g dark chocolate bar` | bare food, no verb or meal word |
| `what's a good pasta recipe?` | polite decline (extractor says not food) |
| `/start`, empty message | polite decline (no LLM call) |
