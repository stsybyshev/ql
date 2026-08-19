# hook-observer POC

A read-only OpenClaw plugin that logs everything four hooks receive, so we can
empirically discover what the current OpenClaw runtime delivers — specifically
whether inbound-message hooks can access photo attachment data.

## What it answers

For the `/eat` architecture decision, we need to know:

1. **Which hook fires first?** (`inbound_claim`, `message_received`, `before_model_resolve`, `before_agent_start` — we register on all four to see the order.)
2. **Do any of them see attachments** (photos of nutrition labels)?
3. **If yes, do they get a local `path` we can read** — or just metadata like MIME type?
4. **Do they get message text alongside the attachment**, so we could implement `/eat` deterministically?

Once we have the log output, we know whether Option 4 (hook-based deterministic `/eat`) is genuinely viable, or blocked the same way `command-dispatch: tool` was.

## Install

1. Ensure OpenClaw is upgraded to 2026.7.x or newer (`openclaw --version`).

2. Add the plugin path to `~/.openclaw/openclaw.json`:
   ```json
   "plugins": {
     "load": {
       "paths": [
         "/home/stan/dev/ql/openclaw-plugins/hook-observer/hook-observer.ts"
       ]
     },
     "entries": { ...your existing entries... }
   }
   ```
   (If a `plugins.load.paths` array already exists, append to it. If it doesn't
   exist, add the whole `plugins.load` block.)

3. Restart the gateway:
   ```bash
   kill $(pgrep -f openclaw-gateway) && openclaw gateway &
   ```

4. Verify the plugin loaded — the log should have a `plugin-init` entry:
   ```bash
   tail -f ~/.openclaw/workspace/hook-observer.log
   ```

## Test

In another WSL terminal, run:
```bash
tail -f ~/.openclaw/workspace/hook-observer.log
```

Then in Telegram, send these three messages one at a time and watch the log:

1. **Plain text:** `just a test message`
2. **Text with photo:** attach any photo, caption "here is a label"
3. **Food-log style:** `just had 3 eggs and a coffee`

For each, we want to see:
- Which hooks fire (order + set)
- What fields are populated in each event
- Whether attachments appear (and how — path? url? mimeType only?)

## Interpreting results

- **Best case:** `inbound_claim` fires with attachment `path` populated → we
  can implement `/eat` fully deterministically (Option 4 wins).
- **Middle case:** attachments show up on `before_model_resolve` with local
  `path` → same conclusion, just uses a different hook.
- **Worst case:** no hook receives a real `path` for photos, only MIME-type
  metadata → hooks can't read the photo bytes directly, kills Option 4 for
  photos (same problem as command-dispatch, back to Options 3 or 5).

## Remove

Delete the entry from `plugins.load.paths` in `openclaw.json` and restart.
The `hook-observer.log` file is safe to keep or delete.
