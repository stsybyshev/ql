"""Router — decides whether a message reaches the extractor at all.

DEFAULT IS "food". The router only short-circuits on messages that carry no
food question worth asking an LLM about: empty text, or a slash command.
Everything else goes to the extractor, which decides what it actually is.

Why it works this way
---------------------
This used to be a regex whitelist of food words (had|ate|breakfast|banana|...).
It declined "200g cherries and 60g dark chocolate bar" — no verb, no meal
word, and `grams?` does not match "200g" — so the message was silently
refused without ever reaching the extractor.

That failure is not fixable by adding words. The whitelist has to enumerate
every food a person might eat, and the list is unbounded: cherries, kipper,
membrillo, sundubu-jigaye. Every food missing from it is a silent decline.

Meanwhile the real classifier already exists downstream and is semantic, not
lexical — the extractor returns `is_food_related`, and graph.py declines on
it. The regex was a lossy pre-filter in front of a working filter.

The trade is cheap and the right way round:
  - false positive (non-food reaches the extractor): one Haiku call, ~$0.003,
    and the user gets the same polite decline they would have got anyway.
  - false negative (food declined by regex): the message is silently lost.
    The user has to guess which magic word unlocks the bot.

Kept as a plain callable, not an ADK @node, so it can be unit-tested trivially.
"""
from __future__ import annotations

import re

# Telegram commands (/start, /help, /reset). These are UI, not food, and the
# extractor has nothing useful to say about them. Matched only at the very
# start of the message so a mid-sentence slash ("100g w/ sauce") is unaffected.
_COMMAND = re.compile(r"^/[a-zA-Z][a-zA-Z0-9_]*")


def route(text: str, has_photo: bool) -> str:
    """Return "food" or "other" for the given event's text + photo state.

    "other" means: reply with the canned decline, spend nothing. It is
    reserved for messages with no content to extract — NOT for messages that
    merely look non-food. That judgement belongs to the extractor.
    """
    if has_photo:
        return "food"
    stripped = (text or "").strip()
    if not stripped:
        return "other"
    if _COMMAND.match(stripped):
        return "other"
    return "food"
