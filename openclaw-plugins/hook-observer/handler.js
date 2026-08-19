/**
 * hook-observer — Round 2: try to influence flow from a HOOK.md hook.
 *
 * We know it observes + gets mediaPath. Now testing whether HOOK.md hooks
 * can (a) mutate the event so the agent sees changed content, (b) short-circuit
 * via handled/block/handled:true style returns.
 *
 * If none of these work, definitive verdict: HOOK.md hooks are pure observers;
 * to short-circuit we need the plugin-SDK path (definePluginEntry).
 */

import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";

const LOG_PATH = path.join(
  os.homedir(),
  ".openclaw/workspace/hook-observer.log",
);

function safeStringify(value) {
  const seen = new WeakSet();
  return JSON.stringify(
    value,
    (_k, v) => {
      if (typeof v === "bigint") return v.toString() + "n";
      if (typeof v === "function") return `[Function ${v.name || "anonymous"}]`;
      if (v && typeof v === "object") {
        if (seen.has(v)) return "[Circular]";
        seen.add(v);
      }
      return v;
    },
    2,
  );
}

async function appendLog(entry) {
  try {
    await fs.mkdir(path.dirname(LOG_PATH), { recursive: true });
    await fs.appendFile(LOG_PATH, entry);
  } catch (err) {
    process.stderr.write(
      `[hook-observer] append failed: ${String(err)}\n`,
    );
  }
}

await appendLog(
  `\n===== ${new Date().toISOString()}  plugin loaded (pid=${process.pid}) round=2 =====\n`,
);

const observer = async (event) => {
  const marker = "[[HOOK-OBSERVER-MUTATION-TEST]]";
  const kind = `${event?.type ?? "?"}:${event?.action ?? "?"}`;

  const before = {
    content_head: (event?.content ?? "").slice(0, 80),
    bodyForAgent_head:
      (event?.context?.bodyForAgent ?? "").slice(0, 80) || "(none)",
    hasMediaPath: Boolean(event?.context?.mediaPath),
  };

  // EXPERIMENT: mutate the event context if this is a preprocessed message.
  // If the agent reply mentions our marker, mutations propagate. If not, hooks
  // are truly observational.
  if (kind === "message:preprocessed" && event?.context) {
    try {
      if (typeof event.context.bodyForAgent === "string") {
        event.context.bodyForAgent = event.context.bodyForAgent + " " + marker;
      }
      if (typeof event.content === "string") {
        event.content = event.content + " " + marker;
      }
    } catch (_e) {}
  }

  const after = {
    content_head: (event?.content ?? "").slice(0, 80),
    bodyForAgent_head:
      (event?.context?.bodyForAgent ?? "").slice(0, 80) || "(none)",
  };

  await appendLog(
    `\n===== ${new Date().toISOString()}  event=${kind}  MUTATION-EXPERIMENT =====\n` +
      `BEFORE mutation:\n${safeStringify(before)}\n` +
      `AFTER mutation:\n${safeStringify(after)}\n`,
  );

  // Also try returning a "handled/block/reply" object to see if runner respects it.
  return {
    handled: true,
    block: true,
    stopPropagation: true,
    reply: {
      text: "[[HOOK-OBSERVER-REPLY-TEST]] if you see this text in Telegram, HOOK.md hooks CAN reply — else they cannot.",
    },
  };
};

export default observer;
