# Agent evals

Fixtures for M8 — imported verbatim from the source-of-truth conversations
at `skills/nutrition-tracker/src/sample-conversations.md`. These are the
cases every prompt change should stay green on.

Files here are **not** loaded by pytest yet. M8 wires them into ADK's
`AgentEvaluator` harness. Keeping them checked in early so:

1. Prompt changes get sanity-tested against real cases now (eyeball).
2. When M8 lands, the eval loop is just "point at this file", not "extract
   cases from a design doc".
