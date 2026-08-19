# ql — Quantified Life Monorepo

This is a private monorepo for personal tracking skills and the universal portal.

## Structure
- `skills/nutrition-tracker/` — OpenClaw food tracking skill + MCP server
- `bots/nutrition-clerk/` — Telegram meal-logging bot (deterministic pipeline)
- `portal/` — Universal dashboard (JSX/HTML, reads precomputed data from Google Drive)

## Working on a specific skill
Open the skill subdirectory in Claude Code for skill-specific context (CLAUDE.md in each skill dir).

## Tests cost real money — know which suite you are running

Some tests in this repo call live LLM APIs on Stan's account. Treat a test
run as a spend decision, not a free action.

| Suite | Cost | Notes |
|---|---|---|
| `tests/unit` | **free** | No network. Passes with `ANTHROPIC_API_KEY` unset. ~2.5s |
| `tests/integration` | **real money** | **measured 19-08-2026: $0.117/run** (87,885 in + 5,726 out on Haiku 4.5 @ $1/$5 per M, 36 LLM calls, 77s). ~$0.005/turn. Skips entirely without a key |

Rules:
- Run `tests/unit` freely, and always before proposing a commit.
- **Ask before running `tests/integration`**, or any live probe script, and
  say roughly what it will cost. Do not run it repeatedly to chase a flaky
  result without saying so first — a "just re-run it a few times" loop is
  how a debugging session quietly costs a pound.
- When adding tests, default to `tests/unit` with the LLM boundary mocked.
  A new live test needs a reason: it must exercise real model behaviour
  (OCR accuracy, classification, estimation quality) that a mock cannot.
- Never wire live tests into a git hook, watcher, or anything automatic.

## Git workflow

Trunk-based: small, green commits straight to `main`. Branch only when a
change cannot land working in one sitting, touches the shared
`food-tracker` MCP layer (Veda depends on it too), or is a throwaway
experiment.

The bot runs from the working tree (`systemd` `ExecStart` points at
`bots/nutrition-clerk`), so **the checked-out branch is what is running in
production**. Switching branches silently redeploys.

Before any commit: run `tests/unit`, then **remind Stan to run them** and
let him decide — do not install a pre-commit hook or run tests
automatically on his behalf.

Commit or push only when asked. Never push without an explicit request.
