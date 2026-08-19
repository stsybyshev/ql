---
name: eat
description: POC for command-dispatch — echoes back whatever the gateway passes to the dispatched tool. Phase 1 stub; tool body becomes the real /eat in Phase 2.
user-invocable: true
command-dispatch: tool
command-tool: eat
command-arg-mode: raw
---

# Eat POC

This skill exists only to validate that `command-dispatch: tool` correctly
routes a slash-command invocation to an MCP-server tool by name, bypassing
the LLM. The body is intentionally never read — dispatch skips it.

Tool: `eat_poc` (in food-cache MCP server). Returns `{command, commandName, skillName}`
echoed back so we can inspect what the dispatcher forwards.
