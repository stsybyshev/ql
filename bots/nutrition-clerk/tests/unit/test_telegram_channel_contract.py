"""Contract tests for TelegramChannel against the installed PTB version.

Why these exist: the channel was written at M1 and only ever exercised
against StubChannel, so a `start_polling(offset=...)` call — valid in PTB
v13, removed in v20+ — sat undetected until the first real launch, where it
raised TypeError before the bot ever connected.

These check our calls against the REAL library signatures without needing a
bot token or network, so a PTB upgrade that moves an argument fails here
rather than at 7am in front of the user.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from nutrition_clerk.channels.telegram import TelegramChannel


def test_start_polling_kwargs_exist_in_installed_ptb():
    """Every kwarg we pass to start_polling must be a real parameter."""
    from telegram.ext import Updater

    params = set(inspect.signature(Updater.start_polling).parameters)
    # Keep in sync with TelegramChannel.start().
    ours = {"allowed_updates"}
    unknown = ours - params
    assert not unknown, (
        f"TelegramChannel.start() passes {unknown} to start_polling(), which "
        f"the installed python-telegram-bot does not accept. Real params: "
        f"{sorted(params - {'self'})}"
    )


def test_offset_is_not_a_start_polling_param():
    """Pins the specific regression: PTB v20+ manages the cursor itself."""
    from telegram.ext import Updater

    params = inspect.signature(Updater.start_polling).parameters
    assert "offset" not in params, (
        "PTB now accepts `offset` again — the comment in TelegramChannel.start() "
        "explaining its removal is stale and should be revisited."
    )


def test_get_file_and_send_message_exist():
    """The other two Bot methods the channel depends on."""
    from telegram import Bot

    assert callable(getattr(Bot, "get_file", None))
    assert callable(getattr(Bot, "send_message", None))


def test_download_to_drive_exists_on_file():
    """Photo download path (SHAPE C/D) depends on this."""
    from telegram import File

    assert callable(getattr(File, "download_to_drive", None))


def test_constructor_requires_a_token(tmp_path):
    with pytest.raises(ValueError, match="token"):
        TelegramChannel(token="", photo_dir=tmp_path)


def test_constructor_creates_photo_dir(tmp_path):
    photo_dir = tmp_path / "nested" / "photos"
    TelegramChannel(token="123:ABC", photo_dir=photo_dir)
    assert photo_dir.is_dir(), "photo_dir should be created eagerly at construction"


def test_media_group_id_exists_on_installed_ptb():
    """Album assembly reads msg.media_group_id — pin it against a PTB upgrade."""
    from telegram import Message
    assert hasattr(Message, "media_group_id"), (
        "python-telegram-bot no longer exposes Message.media_group_id; "
        "TelegramChannel album assembly depends on it."
    )
