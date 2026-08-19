from __future__ import annotations

import logging
from pathlib import Path

from google.adk.agents import BaseAgent
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types

from nutrition_clerk.agents.router import ModelRouter, apply_model_recursively
from nutrition_clerk.channels import InboundEvent
from nutrition_clerk.tools import PENDING_CLARIFICATION_KEY

log = logging.getLogger("nutrition_clerk.pipeline")

APP_NAME = "nutrition-clerk"

SYSTEM_META_TEMPLATE = "[SYSTEM_META: telegram_msg_id={msg_id}]\n\n{text}"

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _guess_mime(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
PREV_CLARIFY_TEMPLATE = (
    "[PREV_CLARIFY: {question}]\n"
    "The message below is the user's answer to that question. Resume the "
    "meal-log flow using their answer.\n\n{text}"
)


def _wrap_with_meta(event: InboundEvent) -> str:
    """Prepend a machine-readable header the agent instructions know to strip.

    The msg_id is kept in the LLM context as observational metadata (useful for
    log-correlation and future prompt refinement) — it is NOT passed into any
    tool argument. Deduplication of redelivered Telegram updates is handled
    entirely by `Idempotency` (see persistence/idempotency.py).

    If we ever want row-level belt-and-braces dedup, the right shape is a
    dedicated `MsgId` column in the food-tracker MD schema — that's an MCP
    server schema evolution, not a repurposing of the `source` column.
    """
    return SYSTEM_META_TEMPLATE.format(msg_id=event.msg_id, text=event.text or "")


class AgentPipeline:
    """Thin wrapper over ADK's Runner. One instance per bot process.

    Turns an InboundEvent into a text reply by running the agent graph.
    Session is keyed on `chat_id` so multi-turn state (added at M5) survives
    across messages from the same user.
    """

    def __init__(
        self,
        root_agent: BaseAgent,
        session_service: BaseSessionService | None = None,
        app_name: str = APP_NAME,
        router: ModelRouter | None = None,
    ) -> None:
        self._app_name = app_name
        self._session_service = session_service or InMemorySessionService()
        self._root_agent = root_agent
        self._router = router
        # Public observability: which routing rule fired on the most recent
        # turn. Useful for tests and cost debugging. None until first handle().
        self.last_route: str | None = None
        self._runner = Runner(
            app_name=app_name,
            agent=root_agent,
            session_service=self._session_service,
            auto_create_session=True,
        )

    async def _read_pending_clarification(self, user_id: str, session_id: str) -> str | None:
        try:
            session = await self._session_service.get_session(
                app_name=self._app_name,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception:  # session may not exist yet — expected on first turn
            return None
        if not session:
            return None
        pending = session.state.get(PENDING_CLARIFICATION_KEY) if session.state else None
        return pending or None

    async def handle(self, event: InboundEvent) -> str:
        user_id = str(event.chat_id)
        session_id = str(event.chat_id)

        # Per-turn model routing: pick a profile based on the event, swap it
        # onto the shared agent tree in place. Same MCP subprocess and same
        # Runner throughout — ADK's LlmAgent.model is a mutable field the
        # Runner reads at invocation time (verified empirically).
        if self._router is not None:
            route_name, model = self._router.resolve(event)
            self.last_route = route_name
            log.info("route %r -> %s", route_name, getattr(model, "model", model))
            apply_model_recursively(self._root_agent, model)
        else:
            self.last_route = None

        text_for_agent = _wrap_with_meta(event)
        pending = await self._read_pending_clarification(user_id, session_id)
        state_delta: dict | None = None
        if pending:
            log.info("resuming clarify for chat_id=%s", event.chat_id)
            text_for_agent = PREV_CLARIFY_TEMPLATE.format(
                question=pending, text=text_for_agent
            )
            # Clear the pending key at turn start; the agent may set a NEW one
            # if it decides to clarify again.
            state_delta = {PENDING_CLARIFICATION_KEY: ""}

        parts: list[types.Part] = [types.Part.from_text(text=text_for_agent)]
        for photo_path in event.photos:
            try:
                data = photo_path.read_bytes()
            except OSError as e:
                log.warning("skipping unreadable photo %s: %s", photo_path, e)
                continue
            parts.append(
                types.Part.from_bytes(
                    data=data, mime_type=_guess_mime(photo_path)
                )
            )
        content = types.Content(role="user", parts=parts)

        final_text = ""
        total_input = 0
        total_output = 0

        async for adk_event in self._runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
            state_delta=state_delta,
        ):
            usage = adk_event.usage_metadata
            if usage is not None:
                total_input += getattr(usage, "prompt_token_count", 0) or 0
                total_output += getattr(usage, "candidates_token_count", 0) or 0

            if adk_event.is_final_response() and adk_event.content:
                parts = adk_event.content.parts or []
                final_text = "".join((p.text or "") for p in parts).strip()

        log.info(
            "turn done: chat_id=%s tokens_in=%d tokens_out=%d final_len=%d",
            event.chat_id,
            total_input,
            total_output,
            len(final_text),
        )

        # Safety nets for two recurring Haiku failure modes around clarify:
        #
        # (1) LLM asks the user a question in natural language but forgets to
        #     call the `clarify` tool. Without the tool call, session state
        #     has no pending_clarification, so next turn's PREV_CLARIFY prefix
        #     never fires and the user's answer is treated as a fresh query.
        #     Detect heuristically (reply ends with '?') and record it.
        #
        # (2) LLM calls `clarify` correctly but treats the tool call as the
        #     full response and produces no natural-language text. The user
        #     would see nothing. Recover the question from the state we just
        #     wrote and use it as the reply.
        pending_now = await self._read_pending_clarification(user_id, session_id)

        if final_text.rstrip().endswith("?") and not pending_now:
            log.warning(
                "clarify tool was NOT called but reply ends with '?' — "
                "auto-recording as pending_clarification. chat_id=%s",
                event.chat_id,
            )
            session = await self._session_service.get_session(
                app_name=self._app_name,
                user_id=user_id,
                session_id=session_id,
            )
            if session and session.state is not None:
                session.state[PENDING_CLARIFICATION_KEY] = final_text

        elif not final_text and pending_now:
            log.warning(
                "agent called clarify but produced no reply — using the "
                "recorded question as reply. chat_id=%s",
                event.chat_id,
            )
            final_text = pending_now

        return final_text or "(no response)"
