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

    async def _on_message(self, update: Update, _context) -> None:
        msg = update.message or update.edited_message
        if not msg:
            return
        text = msg.text or msg.caption or ""
        photos = await self._download_photos(msg)
        await self._inbox.put(
            InboundEvent(
                chat_id=msg.chat_id,
                msg_id=msg.message_id,
                text=text,
                photos=photos,
                update_id=update.update_id,
            )
        )

    async def inbound(self) -> AsyncIterator[InboundEvent]:
        while True:
            yield await self._inbox.get()

    async def send_text(self, chat_id: int, text: str) -> None:
        assert self._app is not None, "TelegramChannel not started"
        await self._app.bot.send_message(chat_id=chat_id, text=text)
