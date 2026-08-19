from __future__ import annotations

from pathlib import Path

from nutrition_clerk.agents.pipeline import _guess_mime


def test_mime_by_suffix():
    assert _guess_mime(Path("x.jpg")) == "image/jpeg"
    assert _guess_mime(Path("x.JPEG")) == "image/jpeg"
    assert _guess_mime(Path("x.png")) == "image/png"
    assert _guess_mime(Path("x.webp")) == "image/webp"
    assert _guess_mime(Path("x.gif")) == "image/gif"


def test_mime_defaults_for_unknown():
    assert _guess_mime(Path("x.tiff")) == "application/octet-stream"
    assert _guess_mime(Path("noext")) == "application/octet-stream"
