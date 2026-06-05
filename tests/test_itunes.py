"""Unit tests for the iTunes cover-art fallback. ``_get`` (the only
network touch-point) is monkeypatched; nothing hits the network."""

from __future__ import annotations

import json

import pytest

from mpdris2 import itunes

_PNG = b"\x89PNG\r\n\x1a\n" + b"img"


def _router(mapping: dict):
    def _get(url: str) -> bytes:
        for key, val in mapping.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"unexpected url: {url}")
    return _get


def _search(artist: str, album: str, art: str | None) -> bytes:
    res: dict = {"artistName": artist, "collectionName": album}
    if art is not None:
        res["artworkUrl100"] = art
    return json.dumps({"results": [res]}).encode()


@pytest.mark.asyncio
async def test_fetch_cover_returns_upscaled_artwork(monkeypatch) -> None:
    monkeypatch.setattr(itunes, "_get", _router({
        "itunes.apple.com/search": _search("A", "B", "https://art/100x100bb.jpg"),
        "600x600": _PNG,
    }))
    assert await itunes.fetch_cover("A", "B") == _PNG


@pytest.mark.asyncio
async def test_fetch_cover_artist_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(itunes, "_get", _router({
        "itunes.apple.com/search": _search("Someone Else", "B", "https://art/100x100bb.jpg"),
    }))
    assert await itunes.fetch_cover("A", "B") is None


@pytest.mark.asyncio
async def test_fetch_cover_no_results(monkeypatch) -> None:
    monkeypatch.setattr(itunes, "_get", _router({"itunes.apple.com/search": b'{"results": []}'}))
    assert await itunes.fetch_cover("A", "B") is None


@pytest.mark.asyncio
async def test_fetch_cover_network_error(monkeypatch) -> None:
    monkeypatch.setattr(itunes, "_get", _router({"itunes.apple.com/search": OSError("boom")}))
    assert await itunes.fetch_cover("A", "B") is None
