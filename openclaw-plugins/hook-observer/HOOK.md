---
name: hook-observer
description: "Read-only diagnostic hook that logs every event it receives (event + full payload) to ~/.openclaw/workspace/hook-observer.log. Never claims / never blocks — safe passthrough for empirical discovery of what OpenClaw hooks deliver."
metadata:
  {
    "openclaw":
      {
        "emoji": "🔎",
        "events":
          [
            "message:received",
            "message:preprocessed",
            "message:transcribed",
            "message:sent",
            "command",
            "gateway:startup"
          ]
      }
  }
---

# hook-observer

Empirical diagnostic hook. For each subscribed event, dumps the entire event
object as JSON to `~/.openclaw/workspace/hook-observer.log` and returns
without claiming or blocking. Safe to install and forget.

## What it answers

For the `/eat` architecture decision, we need to know:

1. Which hook fires first for an inbound Telegram message?
2. Do any of them see attachments (photos of nutrition labels)?
3. If yes, do they get a local `path` we can read — or just MIME-type metadata?
4. Do they get the message text alongside the attachment?

## Install

```bash
openclaw plugins install /home/stan/dev/ql/openclaw-plugins/hook-observer
# restart the gateway
```

## Inspect

```bash
tail -f ~/.openclaw/workspace/hook-observer.log
```

Then send Telegram messages: text-only, then text-with-photo. Compare payloads.

## Remove

```bash
openclaw plugins uninstall hook-observer
```
