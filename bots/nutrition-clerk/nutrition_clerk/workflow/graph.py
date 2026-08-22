"""Pipeline assembly + top-level handler.

Design note: I evaluated wrapping this as an ADK `Workflow(edges=[...])` per
the plan. Hands-on it turned out the ADK Workflow adds meaningful ceremony
(strict edge schema equality, state plumbing for cross-node data, first-node
`node_input` typed to `Content`) that this 4-step pipeline doesn't benefit
from — the whole pipeline is a ~15-line async function.

We keep the `LiteLlm` LLM primitive from ADK (used by extractor + future
enrichers), so we're still "using ADK" for the LLM plumbing. What we skip is
ADK's Runner + Workflow orchestrator layer. If we later want the observability
(events, tracing, replay), we can wrap this in a Workflow — but a pure-Python
`handle_event` is more legible for now.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from google.adk.models.base_llm import BaseLlm

from nutrition_clerk.channels import InboundEvent
from nutrition_clerk.config import Config
from nutrition_clerk.model import build_model
from nutrition_clerk.workflow import context as chat_context
from nutrition_clerk.workflow import extractor, formatter, orchestrator, router, trace
from nutrition_clerk.workflow.food_cache_client import FoodCacheClient
from nutrition_clerk.workflow.prompts import POLITE_DECLINE_TEXT
from nutrition_clerk.workflow.schemas import ExtractedEntry

_PREV_CLARIFY_TEMPLATE = (
    "[PREV_CLARIFY]\n"
    "You asked the user this question:\n"
    "    {question}\n"
    "It was about ONE unresolved food item, which they had described as:\n"
    "    {pending}\n"
    "Their answer is below. Emit EXACTLY ONE entry for that item, combining\n"
    "their answer with the quantity/unit they originally gave. Do NOT re-ask,\n"
    "and do NOT emit entries for any other food.\n\n"
    "{text}"
)


def _describe_pending(entry: dict) -> str:
    """Human-readable one-liner for the unresolved entry, for the prompt."""
    bits = [str(entry.get("name") or "?")]
    qty, unit = entry.get("qty"), entry.get("unit")
    if qty is not None:
        bits.append(f"quantity {qty:g}")
    if unit:
        bits.append(f"unit {unit!r}")
    return ", ".join(bits)

log = logging.getLogger("nutrition_clerk.workflow.graph")

Handler = Callable[[InboundEvent], Awaitable[str]]


async def _handle(
    event: InboundEvent,
    *,
    model: BaseLlm,
    vision_model: BaseLlm,
    knowledge_model: BaseLlm,
    client: FoodCacheClient,
) -> str:
    """Route → extract → orchestrate → format. No ADK Workflow, no session."""
    text = event.text or ""
    has_photo = bool(event.photos)

    # N4: check for a pending clarification BEFORE the router. If we asked
    # the user a question last turn, their answer might not match the food
    # regex ("seeds", "the second one", "cheese") — we still want to route
    # it as food because it resumes an in-flight conversation.
    ctx = chat_context.get_context(event.chat_id)
    prev_question = ctx.pending_clarification
    prev_entries = list(ctx.pending_entries)

    if prev_question is None and router.route(text, has_photo) == "other":
        log.info("router: other -> decline (no LLM call)")
        trace.record("router", route="other")
        return POLITE_DECLINE_TEXT
    trace.record("router", route="food", resuming=bool(prev_question))

    # Tell the extractor a photo is attached. It never sees images, so without
    # this it asks for a quantity that the photo is there to supply.
    extractor_input = text
    if has_photo:
        n = len(event.photos)
        extractor_input = (
            f"[{n} PHOTO{'S' if n > 1 else ''} ATTACHED]\n"
            "A photo accompanies this message. Do NOT ask how much of a food "
            "there is — the photo answers that. Emit the entries with qty/unit "
            "null when the user did not state them.\n\n" + text
        )
    resuming = False
    if prev_question:
        resuming = True
        # Two clarification sources, two resume shapes:
        #  - orchestrator (ambiguous cache hit): we have the unresolved
        #    entries, so resume ONLY those. Replaying the whole message here
        #    would re-log the entries that already succeeded.
        #  - extractor (message too vague): nothing was logged and there are
        #    no entries to queue, so replaying the original text is correct.
        pending_desc = (
            _describe_pending(prev_entries[0])
            if prev_entries
            else (ctx.pending_original_message or "(their previous message)")
        )
        log.info(
            "chat %s: resuming clarify (%r) for %d pending item(s)",
            event.chat_id, prev_question, len(prev_entries),
        )
        extractor_input = _PREV_CLARIFY_TEMPLATE.format(
            question=prev_question,
            pending=pending_desc,
            text=text,
        )
        # Clear immediately — if the orchestrator finds a NEW ambiguity we
        # re-set it below from this turn's result.
        ctx.clear_pending()

    extracted = await extractor.extract(model, extractor_input)

    if resuming:
        # The extractor resolved (at most) the FIRST pending item. Re-queue
        # the remaining pending items behind it so they get processed in this
        # same turn rather than being forgotten.
        leftovers = [ExtractedEntry(**e) for e in prev_entries[1:]]
        extracted.entries = list(extracted.entries) + leftovers
    ctx.touch()
    if not extracted.is_food_related:
        log.info("extractor: is_food_related=false -> decline")
        return POLITE_DECLINE_TEXT

    # N4: extractor-emitted clarification (message too vague to parse).
    if extracted.clarification_question and has_photo:
        # The extractor is TEXT-ONLY, so it cannot see that a photo is attached
        # and will ask "how much?" for a meal it cannot measure. That is the one
        # question a photo exists to answer: "Log my lunch: potato and artichoke
        # salad" + a photo of the plate returned the question and never ran
        # vision at all, because this branch returns before the orchestrator.
        # Photos win — the orchestrator asks its own question if vision fails.
        log.info(
            "extractor asked %r but %d photo(s) are attached — trying vision first",
            extracted.clarification_question, len(event.photos),
        )
        extracted.clarification_question = None

    if extracted.clarification_question:
        log.info(
            "extractor asked for clarification: %r", extracted.clarification_question
        )
        ctx.pending_clarification = extracted.clarification_question
        ctx.pending_original_message = text
        return extracted.clarification_question

    if not extracted.entries:
        return "I couldn't find any food items in that message. Could you rephrase?"

    result = await orchestrator.orchestrate(
        extracted, client,
        photos=list(event.photos),
        vision_model=vision_model,
        knowledge_model=knowledge_model,
        context=ctx,
        message_text=text,
        resuming_clarification=resuming,
    )

    # N4: orchestrator-emitted clarification (multi-hit cache lookup).
    # Everything resolvable in this message has ALREADY been logged by now —
    # only the ambiguous items are outstanding.
    if result.pending_clarification:
        ctx.pending_clarification = result.pending_clarification
        ctx.pending_entries = [e.model_dump() for e in result.unresolved]
        # Always show what did land, so nothing is silently dropped.
        return f"{formatter.format_reply(result)}\n\n{result.pending_clarification}"

    return formatter.format_reply(result)


def build_handler(config: Config) -> tuple[Handler, FoodCacheClient]:
    """Wire the workflow. Returns (handler, cache_client).

    The caller owns the cache_client — must `await client.close()` on shutdown.

    Per-node model selection (N6): each LLM call site can use a different
    profile, configured under `[nodes]`. Empty config values fall back to the
    active profile (`models.default` / NUTRITION_CLERK_PROFILE), so a config
    without a `[nodes]` section behaves exactly as it did before.
    """
    fallback_name = config.models.active_profile_name()
    nodes = config.nodes

    def _pick(configured: str, label: str) -> BaseLlm:
        name = configured or fallback_name
        if name not in config.models.profiles:
            log.warning(
                "%s profile %r not defined; falling back to %r",
                label, name, fallback_name,
            )
            name = fallback_name
        profile = config.models.profiles[name]
        log.info("workflow %-9s profile: %s -> %s", label, name, profile.model)
        return build_model(profile)

    model = _pick(nodes.extractor_profile, "extractor")
    vision_model = _pick(nodes.vision_profile, "vision")
    knowledge_model = _pick(nodes.knowledge_profile, "knowledge")

    client = FoodCacheClient(config.mcp.food_tracker)

    chat_context.configure(
        inactivity_timeout_hours=config.context.inactivity_timeout_hours,
        recent_ring_size=config.context.recent_entries_ring_size,
    )

    state_dir = config.paths.resolved_state_dir()
    trace_cfg = trace.TraceConfig(
        enabled=config.tracing.enabled,
        path=config.tracing.resolved_path(state_dir),
        record_payloads=config.tracing.record_payloads,
        max_payload_chars=config.tracing.max_payload_chars,
    )
    if trace_cfg.enabled:
        log.info("turn tracing -> %s (payloads=%s)",
                 trace_cfg.path, trace_cfg.record_payloads)

    async def handler(event: InboundEvent) -> str:
        with trace.turn(
            chat_id=event.chat_id,
            msg_id=event.msg_id,
            text=event.text or "",
            photos=list(event.photos),
            config=trace_cfg,
        ) as t:
            try:
                reply = await _handle(
                    event,
                    model=model,
                    vision_model=vision_model,
                    knowledge_model=knowledge_model,
                    client=client,
                )
            except Exception:
                # Photos live in the channel's temp dir; keep a copy so a
                # failed photo turn can actually be replayed later.
                if config.tracing.retain_failed_photos and event.photos:
                    kept = _retain_photos(event.photos, state_dir, t.turn_id)
                    if kept:
                        t.input["retained_photos"] = kept
                raise
            t.reply = reply
            return reply

    return handler, client


def _retain_photos(photos: list[Path], state_dir: Path, turn_id: str) -> list[str]:
    """Copy photos somewhere durable so a failed turn stays reproducible."""
    kept: list[str] = []
    dest_dir = state_dir / "failed-turns" / turn_id
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for photo in photos:
            try:
                dest = dest_dir / photo.name
                shutil.copyfile(photo, dest)
                kept.append(str(dest))
            except OSError:
                log.warning("could not retain photo %s", photo, exc_info=True)
    except OSError:
        log.warning("could not create %s", dest_dir, exc_info=True)
    return kept
