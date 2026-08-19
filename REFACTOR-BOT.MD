# Refactoring Plan for extracting "clerical entry" tasks from OpenClaw into a custom agent

## GOAL
The purpose of this major refactoring is twofold:
1. **Cut costs** — instead of dealing with "logging" (meals, finance, gym, etc.) tasks from OpenClaw, switch to an agent with predominantly deterministic workflow and where LLM is used only to parse text and images.
2. **Own the stack + predictable behavior** — a small purpose-built bot means we own the prompt, can log/replay every LLM call, and are not fighting a general-purpose harness for clerical work.

## ISSUES WITH THE CURRENT DESIGN
Right now, we use single OpenClaw agent (Veda) for everything. It has its advantage (single entry channel, ability to set meal reminders linked to meals, etc.) but it also introduce problems. Main one is cost, as even on a cheapest Haiku models, the daily entry costs are around 1-1.5 USD - mainly because OpenClaw is by definition general purpose and it has a ton of features (large system prompt, session history preservation, skills, etc.) that are not only not useful for clerical tasks, but actually counterproductive.
We also explored options with "hacking" deterministic workflow into OpenClaw (hooks, plugins, etc.) - but it looked like we are fighting against the OpenClaw design with these hacks.



## PROPOSED SOLUTION
### Context
Create an ADK 2.0 bot that can run a deterministic workflow and populate MD daily log file

### Requirements
- [ ] Use ADK 2.0 framework (partially due to educational needs, rather than particular preference for this specific framework)
- [ ] Can be run locally now (inside WSL) but can be deployed to Google infrastructure later, if needed
- [ ] Should be smart enough to politely decline general purpose asks (focus on data entry domain)
- [ ] Should be extendable to other domains later (e.g. finance, gym, etc.), so there is 1 bot for clerical tasks
- [ ] Should be pluggable in terms of input channels - can connect to a specific Telegram bot, but later may be called from WhatsApp, Discord or even a custom mobile app
- [ ] Security - avoid exposing any endpoints from a local WSL system where it will run. **Telegram integration is polling only — no inbound HTTP / webhooks.**
- [ ] Should allow for relatively ease model switch and should support running on local Ollama models (Gemma E4B that is already installed in WSL) or cheap cloud models (Haiku)
- [ ] **Idempotency** — Telegram redelivers on offset gaps. Persist the update offset to disk and dedupe on `message_id` so a crash-restart never double-logs a meal.
- [ ] **Reuse existing MCP server** (`skills/nutrition-tracker/mcp-server/food-tracker/`) as-is. Clerk bot is an MCP client; Veda remains an MCP client too.
- [ ] **Runs alongside Veda** — Veda stays as-is (backup + Serc reminders + everything non-meal). Only meal logging migrates to Clerk. No cutover plan needed beyond "stop sending meals to Veda".
- [ ] **New Telegram bot** — register a fresh `@VedaClerkBot` (or similar). Do not reuse `@openclaw_sdt_bot`.
 
### Solution 
The flow should be relatively straightforward:
1. Bot polls Telegram (long-poll, no webhooks). Persists update offset + processed `message_id`s to disk for idempotency.
2. If a new message detected - analyse (grep?) if it relates to one of the supported domains (for now - meal tracking)
3. If not - polite decline
4. If yes - route to the correct node (for now just one - meal tracking)
5. Understand if there are attachments (photos of the label) in the message
6. Run LLM on the message - the goal is to split the message into an array of entries:
    6.1 <Meal, Qty, SaveToCache> - this is for common foods and foods from cache ("1 apple" or "1 serving of sundubu-jigaye")
    6.2 <Meal, Qty, Calories, Protein, Fat, Carbs, SaveToCache> - this is when I bother to enter full data from the label
    6.3 <Meal, Qty, {photo}, SaveToCache> - this is when I dont specify the calories and just attach the photo of label with macros.
        - Photo↔meal matching strategy: **read the product/meal name from the label sticker itself** (Stan will include a photo of the name sticker alongside the macros label). Vision model returns `{detected_meal_name, macros}` per photo; we match on name against the meals extracted from the text.
    6.4 <Meal, SaveToCache> - this is when I instruct to save a recent meal (from chat history) into a personal-foods cache
        - **"Recent meal" context** = re-read the last N entries from the current `YEAR-MONTH.md` log file. No separate session store; the MD log IS the memory.
7. ALWAYS run a search in personal foods and then popular foods for each meal provided - also consider running a "semantic" search, rather than a true textual search - this is via MCP call
    - Simplest sufficient approach: normalise (lowercase, strip punctuation, singularise) + fuzzy match (e.g. `rapidfuzz` ratio > threshold) against personal-foods keys. Goal is dedup on typos, not full semantic recall.
8. If there is a SaveToCache flag or separate instruction - save it into personal-foods, but check if the food with a same or similar name is already there - then update it (rather than create duplicate entries)
9. Another MCP call to save it into YEAR-MONTH.MD file

### Failure UX
When parsing is low-confidence, ambiguous, or the fuzzy-match returns a candidate below a certainty threshold — **ask back** in Telegram with the specific ambiguity (e.g. "did you mean *cashews* (existing) or a new entry *cashew nuts*?"). Never auto-log when uncertain.

## BACKLOG
- [ ] Extract a set of tests from the recent OpenClaw history - for all eventualities above + some artificial examples for edge cases
- [ ] Create an ADK bot with the flow above and a config file (to connect to a specific model, connection to particular Telegram channel - all sensible to extract config params)
- [ ] Create an technical design doc, explaining the ADK source code (educational purpose)
- [ ] Create an deployment script (one time, systemd) and deployment instruction