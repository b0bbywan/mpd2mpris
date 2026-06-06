"""Unit tests for the iTunes cover-art fallback. ``_get`` (the only
network touch-point — the search call) is monkeypatched; nothing hits
the network. ``cover_url`` returns the artwork URL, no image download."""

from __future__ import annotations

import json

import pytest

from mpdris2 import itunes


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
async def test_cover_url_returns_upscaled_artwork(monkeypatch) -> None:
    monkeypatch.setattr(itunes, "_get", _router({
        "itunes.apple.com/search": _search("A", "B", "https://art/100x100bb.jpg"),
    }))
    assert await itunes.cover_url("A", "B") == "https://art/600x600bb.jpg"


@pytest.mark.asyncio
async def test_cover_url_artist_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(itunes, "_get", _router({
        "itunes.apple.com/search": _search("Someone Else", "B", "https://art/100x100bb.jpg"),
    }))
    assert await itunes.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_no_results(monkeypatch) -> None:
    monkeypatch.setattr(itunes, "_get", _router({"itunes.apple.com/search": b'{"results": []}'}))
    assert await itunes.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_network_error(monkeypatch) -> None:
    monkeypatch.setattr(itunes, "_get", _router({"itunes.apple.com/search": OSError("boom")}))
    assert await itunes.cover_url("A", "B") is None
