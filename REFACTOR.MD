# Refactoring Plan for QL skills and setting foundation for further growth

## GOAL
The purpose of this major refactoring is to resolve a genuine dilemma. The current setup with food-tracking has parts that are working well (nice HTML dashboard accessible via Google Drive sync) and those that dont. The main challenge is that "clerical data entry" task doesnt suit the main OpenClaw loop. It just adds negative value - as the whole OpenClaw infra  (soul and identity md files, subagents, session management) are just eating tokens for something that is very  simple and mostly deterministic (read user message, remove fluff and get meaningful bits - food names, quantity, photos, call MCP server to find food in cache and register, call LLM model if a new food or photo label). Clearly 

## OPENCLAW PROPOSED FIX
The proposed alternative (in swimming-duckling.md) is to split the tasks. Create 1 custom mostly deterministic Python bot outside OpenClaw for "clerical data entry" with mostly deterministic workflow. Keep OpenClaw skill for just managing this Python bot and generating dashboard, identify trends and other "high value add" activities.

### Challenges of that solution:
 - Another piece of code to maintain (custom Python bot)
 - Another piece of infra to orchestrate (ensure it is rerun on WSL restart and so on)
 - Harder to package and publish on OpenClaw (2 pieces - one is inside OpenClaw stack, one is outside) - and harder to install for non-technical users of OpenClaw

## PROPOSED SOLUTION
### Context
The challenges of the OpenClaw setup getting in the way are real - for mostly deterministic tasks it only adds latency and costs. The challenges of alternative proposed fix are also real.
The best-of-both worlds is something that avoids the hassle of additional Python bot to manage, but also drastically reduces latency and costs.

### Solution
Instead of creating a custom Python bot for "clerical" tasks, keep using the same OpenClaw telegram bot, but make it explicitly support the "deterministic and skip the main agent model" mode.
For that - introduce the same skill, but with a clear telegram command prefix - like /eat. As per documentation in https://docs.openclaw.ai/tools/slash-commands#skill-commands-and-native-arguments there is a way to do so ("skills may optionally declare command-dispatch: tool to route the command directly to a tool (deterministic, no model)") - that should serve the same purpose as having a separate Telegram bot working outside of the loop of normal agent. 
If we implement these /eat skill (and later /gym, /spend, /sleep and so on) - we can keep using the same single Telegram bot, but we can route the command text to a tool that can be as deterministic as we want:
* Analyze text
* Make a call to a local model or Haiku with a super simple request ("this is about food. Remove all fluff and keep only {food_name, qty, photo_attached_true}, then pass each to the MCP server configured, then either use MCP verb to save it or make another call to Haiku/GemmaE4B to estimate from photo label or from photo of meal in a restaurant or from just textual description")
* Main OpenClaw agent can run on expensive model for more value add and adhoc requests

This approach should keep it future extensible ( later /gym, /spend, /sleep and so on ) and publishable in OpenClaw marketplace and relatively easy to install for non-technical people. 

### Open desing questions
The design dilemma is how exactly we implement this tool for "deterministic flow" - another MCP verb to make it callable from skill? just custom bash script? something else (OpenProse script?). That is something that should be relatively simple (essentially a parser + orchestration workflow), but something easily callable from OpenClaw skill

## Phase 0: Analyze OpenClaw documentation
- [ ] Read docs on Tools and skills - https://docs.openclaw.ai/tools
- [ ] Understand how to create a skill that can skip the LLM ("skills may optionally declare command-dispatch: tool to route the command directly to a tool (deterministic, no model)")  https://docs.openclaw.ai/tools/slash-commands#skill-commands-and-native-arguments
- [ ] Understand how OpenProse skill work as an example - it also uses command-dispatch mechanic

## Phase 1: Create a POC
Before fully committing to refactoring, we need to check if 
- [ ] Design a skill that has command-dispatch: tool and can route the invocation to a custom piece of code (Python?) that can for now just parse the input text into JSON friendly format (that is easy to pass to MCP food skill in the future but for now just prints it) 
- [ ] Create a manifest for this new skill that can be deployed to OpenClaw easily
- [ ] Deploy this skill and restart Veda - see if it works or command-dispatch has some limitations that would kill this approach


## Phase 2: Refactor
- [ ] Create a new skill - /eat - that should have command-dispatch: tool
- [ ] Design an implementation approach for the tool (mostly deterministic) - Python code, another MCP verb ("parse eat skill")
- [ ] Implement admin layer for the skill and tool - config parameter for choice of model for food estimation and photo label OCR (so it should work with Haiku or local Gemma E4B) and secure access to API keys for both
- [ ] Implement tool
- [ ] Deploy this new /eat skill
- [ ] Run tests
- [ ] Create a publishable "plugin" - that packs everything (skill, tool, assets)


## Phase 3: Further improvements
- [ ] Make OpenClaw run as WSL systemd service - so it successully survives WSL restarts
- [ ] Support some quick combo templates (instead of entering "Today I had morning espresso and 1 tsp of MCT 8 oil" every day, there should be a configurable shortcut like "/eat morning" or something similar)


## Phase 4: Clean up 
If all goes well for a couple of weeks of real life test
- [ ] Remove old skill that is directly installed in OpenClaw
- [ ] Make a statement for OpenClaw main agent - so it is aware roughly where the monthly .MD files are stored and can fix something adhoc in case of problems.