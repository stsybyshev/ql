from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable, Callable

from nutrition_clerk.agents import (
    AgentPipeline,
    ModelRouter,
    build_meal_agent,
    build_polite_decline_agent,
    build_root_agent,
)
from nutrition_clerk.channels import Channel, InboundEvent
from nutrition_clerk.channels.stub import StubChannel
from nutrition_clerk.channels.telegram import TelegramChannel
from nutrition_clerk.config import Config, load_config
from nutrition_clerk.model import build_model
from nutrition_clerk.persistence import Idempotency
from nutrition_clerk.tools import build_food_mcp_toolset

log = logging.getLogger("nutrition_clerk")

Handler = Callable[[InboundEvent], Awaitable[str]]


async def echo_handler(event: InboundEvent) -> str:
    """M1 placeholder handler; still used by tests to avoid live LLM calls."""
    body = event.text or "(no text)"
    return f"echo: {body}"


async def run(config: Config, channel: Channel, handler: Handler) -> None:
    state_dir = config.paths.resolved_state_dir()
    inbox = Idempotency(state_dir / "inbox.json")
    log.info(
        "nutrition-clerk starting: channel=%s, allowlist=%s, resume offset=%d",
        config.bot.channel,
        config.bot.allowed_chat_ids or "(empty — all dropped)",
        inbox.offset,
    )

    await channel.start()
    try:
        async for event in channel.inbound():
            if event.chat_id not in config.bot.allowed_chat_ids:
                log.info("drop: chat_id=%s not allowlisted", event.chat_id)
                continue
            if inbox.seen(event.msg_id):
                log.info("drop: msg_id=%s already seen", event.msg_id)
                continue

            try:
                reply = await handler(event)
            except Exception:
                # Full context — a bare msg_id told us nothing about which
                # message actually broke. The turn trace (turns.jsonl) has the
                # per-node detail; this is the at-a-glance version.
                log.exception(
                    "handler failed: chat_id=%s msg_id=%s photos=%s text=%r",
                    event.chat_id,
                    event.msg_id,
                    [str(p) for p in event.photos] or "none",
                    (event.text or "")[:200],
                )
                # Do NOT commit — let the platform redeliver so we retry.
                continue
            await channel.send_text(event.chat_id, reply)

            offset = (event.update_id + 1) if event.update_id is not None else 0
            inbox.commit(offset, event.msg_id)
    finally:
        await channel.stop()


def _build_channel(config: Config) -> Channel:
    if config.bot.channel == "telegram":
        return TelegramChannel(
            token=config.bot.telegram_token,
            offset=0,
            photo_dir=config.paths.resolved_state_dir() / "photos",
        )
    return StubChannel()


def build_agent_pipeline(config: Config) -> AgentPipeline:
    """Assemble the ADK agent graph and return a handler-friendly pipeline.

    Behavior depends on whether `models.routing` is empty:
    - Empty (default): single-model pipeline using the active profile
      (defaults to `cloud_haiku`; env override NUTRITION_CLERK_PROFILE).
    - Non-empty: per-turn dispatch — the same agent tree stays alive, but
      its model is swapped in place at each turn based on the first
      routing rule that matches.
    """
    router: ModelRouter | None = None
    if config.models.routing:
        router = ModelRouter(config.models)
        log.info(
            "routing enabled: %d rule(s); default profile %r",
            len(config.models.routing),
            config.models.default,
        )
        # Use the default profile as the seed model — router will swap on first turn.
        initial_model = build_model(config.models.profiles[config.models.default])
    else:
        profile = config.models.active_profile()
        log.info("single-model mode: %s -> %s", config.models.active_profile_name(), profile.model)
        initial_model = build_model(profile)

    food_tools = build_food_mcp_toolset(config.mcp.food_tracker)
    polite = build_polite_decline_agent(initial_model)
    meal = build_meal_agent(
        initial_model,
        toolsets=[food_tools],
        log_dir=config.mcp.food_tracker.resolved_log_dir(),
    )
    root = build_root_agent(initial_model, sub_agents=[meal, polite])
    return AgentPipeline(root_agent=root, router=router)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    def _stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover - Windows / restricted envs
            pass


async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # SECURITY: httpx logs every request URL at INFO, and the Telegram bot
    # token is embedded in the path (.../bot<TOKEN>/getUpdates). At one poll
    # per ~10s that writes the token to the journal continuously, and into
    # any log file shared for debugging. Raise its level so only real
    # failures surface.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # LiteLLM narrates every completion at INFO; the useful token counts are
    # already on our own logger and in turns.jsonl.
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    config = load_config()
    channel = _build_channel(config)

    # N1 pivot: prefer the new workflow handler. Old build_agent_pipeline path
    # kept alive for tests during N1-N6 migration; deleted at N7.
    from nutrition_clerk.workflow import build_handler

    handler, cache_client = build_handler(config)
    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    runner = asyncio.create_task(run(config, channel, handler))
    stopper = asyncio.create_task(stop_event.wait())
    try:
        done, pending = await asyncio.wait(
            {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc:
                raise exc
    finally:
        await cache_client.close()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
