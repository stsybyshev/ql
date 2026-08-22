"""Telegram albums must arrive as ONE event.

21-08-2026: "Prawn gyoza 500g (1st photo), 110g super green falafel (2nd
photo), 500g Heineken..." was sent as an album. Telegram delivers an album as
SEPARATE updates sharing a media_group_id, with the caption on only one of
them, so the bot saw:

    msg 84  "Prawn gyoza 500g (1st photo), ..."  + 1 photo
    msg 85  ""                                   + 1 photo

The extractor correctly parsed photo_index 0 and 1, but only one photo existed,
so attribution was ambiguous and nothing was logged; the second update drew
"I couldn't find any food items in that message."

These tests drive TelegramChannel._on_message directly with fake Update/Message
objects — no token, no network.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nutrition_clerk.channels.telegram import TelegramChannel


def _msg(msg_id, *, chat_id=99, caption="", group=None, photo=True):
    return SimpleNamespace(
        message_id=msg_id, chat_id=chat_id, text=None, caption=caption,
        media_group_id=group, photo=[SimpleNamespace(file_id=f"f{msg_id}")] if photo else [],
        document=None,
    )


def _update(msg, update_id):
    return SimpleNamespace(message=msg, edited_message=None, update_id=update_id)


@pytest.fixture
def channel(tmp_path, monkeypatch):
    ch = TelegramChannel(token="123:ABC", photo_dir=tmp_path)
    # Stub the download so no network/app is needed.
    async def _fake_download(msg):
        return [tmp_path / f"{msg.message_id}.jpg"] if msg.photo else []
    monkeypatch.setattr(ch, "_download_photos", _fake_download)
    ch._GROUP_WINDOW_S = 0.05          # keep the test fast
    return ch


@pytest.mark.asyncio
async def test_album_becomes_one_event_with_every_photo(channel):
    """The real failure: two album parts, one caption."""
    caption = "Prawn gyoza 500g (1st photo), 110g super green falafel (2nd photo)"
    await channel._on_message(_update(_msg(84, caption=caption, group="G1"), 500), None)
    await channel._on_message(_update(_msg(85, caption="", group="G1"), 501), None)

    assert channel._inbox.empty(), "emitted before the album was complete"
    await asyncio.sleep(0.15)

    ev = channel._inbox.get_nowait()
    assert channel._inbox.empty(), "album produced more than one event"
    assert len(ev.photos) == 2, "the second album photo was lost"
    assert ev.text == caption, "the caption did not survive assembly"
    # Must commit past BOTH updates or the unemitted one is redelivered forever.
    assert ev.update_id == 501
    assert ev.chat_id == 99


@pytest.mark.asyncio
async def test_photos_keep_album_order(channel):
    """"1st photo" must mean the first one, whatever order updates arrive in."""
    await channel._on_message(_update(_msg(91, caption="", group="G2"), 600), None)
    await channel._on_message(_update(_msg(90, caption="two", group="G2"), 601), None)
    await asyncio.sleep(0.15)
    ev = channel._inbox.get_nowait()
    assert [p.name for p in ev.photos] == ["90.jpg", "91.jpg"]


@pytest.mark.asyncio
async def test_single_photo_is_emitted_immediately(channel):
    """No media_group_id — the common path must not be delayed or buffered."""
    await channel._on_message(_update(_msg(70, caption="200g cheese"), 700), None)
    ev = channel._inbox.get_nowait()          # no sleep: must be synchronous
    assert ev.text == "200g cheese"
    assert len(ev.photos) == 1
    assert ev.update_id == 700


@pytest.mark.asyncio
async def test_two_albums_do_not_bleed_into_each_other(channel):
    await channel._on_message(_update(_msg(10, caption="first", group="A"), 800), None)
    await channel._on_message(_update(_msg(20, caption="second", group="B"), 801), None)
    await channel._on_message(_update(_msg(11, caption="", group="A"), 802), None)
    await asyncio.sleep(0.15)

    events = {}
    while not channel._inbox.empty():
        e = channel._inbox.get_nowait()
        events[e.text] = e
    assert set(events) == {"first", "second"}
    assert len(events["first"].photos) == 2
    assert len(events["second"].photos) == 1


@pytest.mark.asyncio
async def test_stop_flushes_a_pending_album(channel):
    """A stop mid-window must not strand photos in the buffer."""
    await channel._on_message(_update(_msg(31, caption="dinner", group="C"), 900), None)
    assert channel._inbox.empty()
    await channel.stop()                       # _app is None; must still flush
    ev = channel._inbox.get_nowait()
    assert ev.text == "dinner" and len(ev.photos) == 1
