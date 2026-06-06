"""Unit tests for the Deezer cover-art fallback. ``_get`` (the only
network touch-point — the search call) is monkeypatched; nothing hits
the network. ``cover_url`` returns the cover URL, no image download."""

from __future__ import annotations

import json

import pytest

from mpdris2 import deezer


def _router(mapping: dict):
    def _get(url: str) -> bytes:
        for key, val in mapping.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"unexpected url: {url}")
    return _get


def _search(artist: str, album: str, cover: str | None) -> bytes:
    item: dict = {"title": album, "artist": {"name": artist}}
    if cover is not None:
        item["cover_big"] = cover
    return json.dumps({"data": [item]}).encode()


@pytest.mark.asyncio
async def test_cover_url_returns_url(monkeypatch) -> None:
    monkeypatch.setattr(deezer, "_get", _router({
        "api.deezer.com/search/album": _search("A", "B", "https://cdn/cover.jpg"),
    }))
    assert await deezer.cover_url("A", "B") == "https://cdn/cover.jpg"


@pytest.mark.asyncio
async def test_cover_url_artist_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(deezer, "_get", _router({
        "api.deezer.com/search/album": _search("Someone Else", "B", "https://cdn/cover.jpg"),
    }))
    assert await deezer.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_no_results(monkeypatch) -> None:
    monkeypatch.setattr(deezer, "_get", _router({"api.deezer.com/search/album": b'{"data": []}'}))
    assert await deezer.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_network_error(monkeypatch) -> None:
    monkeypatch.setattr(deezer, "_get", _router({"api.deezer.com/search/album": OSError("boom")}))
    assert await deezer.cover_url("A", "B") is None


def _track(artist: str, track: str, album: str) -> bytes:
    return json.dumps({"data": [{"title": track, "artist": {"name": artist}, "album": {"title": album}}]}).encode()


@pytest.mark.asyncio
async def test_resolve_album_returns_artist_album(monkeypatch) -> None:
    monkeypatch.setattr(deezer, "_get", _router({
        "search/track": _track("Dan Bawaka.Z", "RASTA DUB", "Terre Mère"),
    }))
    assert await deezer.resolve_album("Dan Bawaka.z - Rasta Dub") == ("Dan Bawaka.Z", "Terre Mère")


@pytest.mark.asyncio
async def test_resolve_album_artist_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(deezer, "_get", _router({
        "search/track": _track("Someone Else", "Rasta Dub", "Other Album"),
    }))
    assert await deezer.resolve_album("Dan Bawaka.z - Rasta Dub") is None


@pytest.mark.asyncio
async def test_resolve_album_unparseable_title_skips_query(monkeypatch) -> None:
    # No `` - `` separator: never query, so _get must not be called.
    monkeypatch.setattr(deezer, "_get", _router({"never": b""}))
    assert await deezer.resolve_album("bare station name") is None


@pytest.mark.asyncio
async def test_resolve_album_no_results(monkeypatch) -> None:
    monkeypatch.setattr(deezer, "_get", _router({"search/track": b'{"data": []}'}))
    assert await deezer.resolve_album("A - B") is None


@pytest.mark.asyncio
async def test_resolve_album_network_error(monkeypatch) -> None:
    monkeypatch.setattr(deezer, "_get", _router({"search/track": OSError("boom")}))
    assert await deezer.resolve_album("A - B") is None
