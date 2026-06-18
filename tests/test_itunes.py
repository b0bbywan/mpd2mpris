"""Unit tests for the iTunes cover-art fallback. ``_get`` (the only
network touch-point — the search call) is monkeypatched; nothing hits
the network. ``cover_url`` returns the artwork URL, no image download."""

from __future__ import annotations

import json

import pytest

from mpd2mpris import itunes


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
    monkeypatch.setattr("mpd2mpris._http.get", _router({
        "itunes.apple.com/search": _search("A", "B", "https://art/100x100bb.jpg"),
    }))
    assert await itunes.cover_url("A", "B") == "https://art/600x600bb.jpg"


@pytest.mark.asyncio
async def test_cover_url_upscales_only_trailing_size_token(monkeypatch) -> None:
    # A ``100x100`` earlier in the CDN path must be left untouched; only the
    # trailing size segment is swapped.
    monkeypatch.setattr("mpd2mpris._http.get", _router({
        "itunes.apple.com/search": _search("A", "B", "https://art/100x100/x/100x100bb.jpg"),
    }))
    assert await itunes.cover_url("A", "B") == "https://art/100x100/x/600x600bb.jpg"


@pytest.mark.asyncio
async def test_cover_url_passthrough_when_no_size_token(monkeypatch) -> None:
    # An artwork URL without the ``100x100`` token is served unchanged rather
    # than mangled — the rpartition finds no separator.
    monkeypatch.setattr("mpd2mpris._http.get", _router({
        "itunes.apple.com/search": _search("A", "B", "https://art/cover.jpg"),
    }))
    assert await itunes.cover_url("A", "B") == "https://art/cover.jpg"


@pytest.mark.asyncio
async def test_cover_url_no_artwork_field(monkeypatch) -> None:
    # A hit that matches the artist but carries no artworkUrl100 is a miss.
    monkeypatch.setattr("mpd2mpris._http.get", _router({
        "itunes.apple.com/search": _search("A", "B", None),
    }))
    assert await itunes.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_artist_mismatch(monkeypatch) -> None:
    monkeypatch.setattr("mpd2mpris._http.get", _router({
        "itunes.apple.com/search": _search("Someone Else", "B", "https://art/100x100bb.jpg"),
    }))
    assert await itunes.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_no_results(monkeypatch) -> None:
    monkeypatch.setattr("mpd2mpris._http.get", _router({"itunes.apple.com/search": b'{"results": []}'}))
    assert await itunes.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_network_error_propagates(monkeypatch) -> None:
    # A transient error must propagate (not become None) so cover.py can
    # skip caching and retry later.
    monkeypatch.setattr("mpd2mpris._http.get", _router({"itunes.apple.com/search": OSError("boom")}))
    with pytest.raises(OSError):
        await itunes.cover_url("A", "B")


@pytest.mark.asyncio
async def test_cover_for_track_returns_upscaled_artwork(monkeypatch) -> None:
    monkeypatch.setattr("mpd2mpris._http.get", _router({
        "itunes.apple.com/search": _search("A", "Song", "https://art/100x100bb.jpg"),
    }))
    assert await itunes.cover_for_track("A", "Song") == "https://art/600x600bb.jpg"


@pytest.mark.asyncio
async def test_cover_for_track_artist_mismatch(monkeypatch) -> None:
    monkeypatch.setattr("mpd2mpris._http.get", _router({
        "itunes.apple.com/search": _search("Someone Else", "Song", "https://art/100x100bb.jpg"),
    }))
    assert await itunes.cover_for_track("A", "Song") is None
