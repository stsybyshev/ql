from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, MessageHandler, filters

from nutrition_clerk.channels.base import Channel, InboundEvent

log = logging.getLogger("nutrition_clerk.channels.telegram")


class TelegramChannel(Channel):
    """python-telegram-bot polling channel.

    Uses long-polling only (no webhook). Persisted `offset` is passed at start()
    so redelivered updates land on the app; the outer loop deduplicates via
    msg_id.

    Photo attachments are downloaded to `photo_dir` at receive time; downstream
    consumers see the local Path in `InboundEvent.photos`. Only the largest
    resolution of each photo is fetched (Telegram sends multiple sizes).
    """

    def __init__(
        self,
        token: str,
        *,
        photo_dir: Path,
        offset: int = 0,
    ) -> None:
        if not token:
            raise ValueError("TelegramChannel requires a non-empty bot token")
        self._token = token
        self._offset = offset
        self._photo_dir = photo_dir
        self._photo_dir.mkdir(parents=True, exist_ok=True)
        # Album assembly — see _on_message. Keyed by Telegram's media_group_id.
        # Each member is (msg_id, update_id, chat_id, caption, photos).
        self._groups: dict[str, list[tuple[int, int, int, str, list[Path]]]] = {}
        self._group_timers: dict[str, asyncio.Task] = {}
        self._inbox: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._app: Application | None = None

    async def start(self) -> None:
        self._app = ApplicationBuilder().token(self._token).build()
        self._app.add_handler(MessageHandler(filters.ALL, self._on_message))
        await self._app.initialize()
        await self._app.start()
        # NOTE: python-telegram-bot v20+ owns the polling cursor internally —
        # `start_polling()` has no `offset` parameter (that was the v13 API).
        # Passing one raises TypeError at startup.
        #
        # We lose nothing: deduplication has always really come from the
        # `seen` ring in inbox.json, which is checked before any work and is
        # covered by tests. The persisted offset stays recorded as a debugging
        # aid, but it is no longer the mechanism.
        await self._app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
        )

    async def stop(self) -> None:
        # Flush any album still inside its window. Without this a stop mid-album
        # strands the buffered photos: their updates are never committed, so
        # Telegram redelivers them on restart as captionless messages.
        for group_id in list(self._groups):
            timer = self._group_timers.pop(group_id, None)
            if timer:
                timer.cancel()
            await self._flush_group(group_id)
        if not self._app:
            return
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    async def _download_photos(self, msg) -> list[Path]:
        """Fetch attached photos to disk. Returns local paths."""
        assert self._app is not None
        paths: list[Path] = []
        # Telegram delivers one photo per message but at multiple resolutions —
        # the largest is `msg.photo[-1]`.
        if msg.photo:
            largest = msg.photo[-1]
            file = await self._app.bot.get_file(largest.file_id)
            path = self._photo_dir / f"{msg.chat_id}_{msg.message_id}_{largest.file_unique_id}.jpg"
            try:
                await file.download_to_drive(path)
                paths.append(path)
            except Exception:
                log.exception("failed to download telegram photo %s", largest.file_id)
        # A single-image "document" upload (user attached the raw file rather
        # than compressed photo) — support common image mime types.
        if msg.document and (msg.document.mime_type or "").startswith("image/"):
            doc = msg.document
            file = await self._app.bot.get_file(doc.file_id)
            suffix = Path(doc.file_name or ".bin").suffix or ".bin"
            path = self._photo_dir / f"{msg.chat_id}_{msg.message_id}_{doc.file_unique_id}{suffix}"
            try:
                await file.download_to_drive(path)
                paths.append(path)
            except Exception:
                log.exception("failed to download telegram document %s", doc.file_id)
        return paths

    # An album sent from the Telegram client arrives as SEPARATE updates that
    # share a media_group_id, and only one of them carries the caption. Treating
    # them independently meant "Prawn gyoza 500g (1st photo), 110g falafel (2nd
    # photo)" reached the bot as one text+photo message plus a captionless photo:
    # the second label was never attributed to anything, and the bare photo drew
    # "I couldn't find any food items in that message."
    #
    # So buffer members of a group briefly and emit ONE event carrying every
    # photo and the single caption. Album parts arrive near-simultaneously;
    # the window only has to outlast their download.
    _GROUP_WINDOW_S = 2.0
    _GROUP_MAX = 10  # Telegram's own cap on an album

    async def _flush_group(self, group_id: str) -> None:
        """Emit one event for a completed album."""
        members = self._groups.pop(group_id, [])
        self._group_timers.pop(group_id, None)
        if not members:
            return
        # Album order is message order; sort so photo N matches "Nth photo".
        members.sort(key=lambda m: m[0])
        photos = [p for *_, paths in members for p in paths]
        caption = next((t for *_, t, _ in members if t), "")
        msg_id, _, chat_id, _, _ = members[0]
        # Commit past EVERY update in the group, or the un-emitted ones are
        # redelivered forever.
        update_id = max(u for _, u, _, _, _ in members)
        if len(members) > 1:
            log.info(
                "assembled album %s: %d message(s), %d photo(s), caption=%r",
                group_id, len(members), len(photos), caption[:60],
            )
        await self._inbox.put(
            InboundEvent(
                chat_id=chat_id,
                msg_id=msg_id,
                text=caption,
                photos=photos,
                update_id=update_id,
            )
        )

    async def _group_timer(self, group_id: str) -> None:
        try:
            await asyncio.sleep(self._GROUP_WINDOW_S)
        except asyncio.CancelledError:
            return
        await self._flush_group(group_id)

    async def _on_message(self, update: Update, _context) -> None:
        msg = update.message or update.edited_message
        if not msg:
            return
        text = msg.text or msg.caption or ""
        photos = await self._download_photos(msg)

        group_id = getattr(msg, "media_group_id", None)
        if not group_id:
            await self._inbox.put(
                InboundEvent(
                    chat_id=msg.chat_id,
                    msg_id=msg.message_id,
                    text=text,
                    photos=photos,
                    update_id=update.update_id,
                )
            )
            return

        members = self._groups.setdefault(group_id, [])
        members.append(
            (msg.message_id, update.update_id, msg.chat_id, text, photos)
        )

        # Restart the window on each arrival, then flush once it goes quiet.
        timer = self._group_timers.pop(group_id, None)
        if timer:
            timer.cancel()
        if len(members) >= self._GROUP_MAX:
            await self._flush_group(group_id)
        else:
            self._group_timers[group_id] = asyncio.create_task(
                self._group_timer(group_id)
            )

    async def inbound(self) -> AsyncIterator[InboundEvent]:
        while True:
            yield await self._inbox.get()

    async def send_text(self, chat_id: int, text: str) -> None:
        assert self._app is not None, "TelegramChannel not started"
        await self._app.bot.send_message(chat_id=chat_id, text=text)
