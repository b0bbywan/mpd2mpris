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
    monkeypatch.setattr("mpdris2._http.get", _router({
        "api.deezer.com/search/album": _search("A", "B", "https://cdn/cover.jpg"),
    }))
    assert await deezer.cover_url("A", "B") == "https://cdn/cover.jpg"


@pytest.mark.asyncio
async def test_cover_url_artist_mismatch(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", _router({
        "api.deezer.com/search/album": _search("Someone Else", "B", "https://cdn/cover.jpg"),
    }))
    assert await deezer.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_no_results(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", _router({"api.deezer.com/search/album": b'{"data": []}'}))
    assert await deezer.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_no_cover_field(monkeypatch) -> None:
    # Album hit matches the artist but has neither cover_big nor cover_xl.
    monkeypatch.setattr("mpdris2._http.get", _router({
        "api.deezer.com/search/album": _search("A", "B", None),
    }))
    assert await deezer.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_null_artist(monkeypatch) -> None:
    # A hit with ``"artist": null`` must be a clean miss, not an AttributeError.
    monkeypatch.setattr("mpdris2._http.get", _router({
        "api.deezer.com/search/album": b'{"data": [{"title": "B", "artist": null}]}',
    }))
    assert await deezer.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_network_error_propagates(monkeypatch) -> None:
    # A transient error must propagate (not become None) so cover.py can
    # skip caching and retry later.
    monkeypatch.setattr("mpdris2._http.get", _router({"api.deezer.com/search/album": OSError("boom")}))
    with pytest.raises(OSError):
        await deezer.cover_url("A", "B")


def _track(artist: str, track: str, album: str, cover: str | None = "https://cdn/t-cover.jpg") -> bytes:
    a: dict = {"title": album}
    if cover is not None:
        a["cover_big"] = cover
    return json.dumps({"data": [{"title": track, "artist": {"name": artist}, "album": a}]}).encode()


@pytest.mark.asyncio
async def test_cover_for_track_returns_cover(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", _router({
        "search/track": _track("Dan Bawaka.Z", "RASTA DUB", "Terre Mère", "https://cdn/t.jpg"),
    }))
    assert await deezer.cover_for_track("Dan Bawaka.z", "Rasta Dub") == "https://cdn/t.jpg"


@pytest.mark.asyncio
async def test_cover_for_track_artist_mismatch(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", _router({
        "search/track": _track("Someone Else", "Rasta Dub", "Other Album"),
    }))
    assert await deezer.cover_for_track("Dan Bawaka.z", "Rasta Dub") is None


@pytest.mark.asyncio
async def test_cover_for_track_no_results(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", _router({"search/track": b'{"data": []}'}))
    assert await deezer.cover_for_track("A", "B") is None


@pytest.mark.asyncio
async def test_cover_for_track_no_cover(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", _router({"search/track": _track("A", "B", "Album", cover=None)}))
    assert await deezer.cover_for_track("A", "B") is None


@pytest.mark.asyncio
async def test_cover_for_track_null_artist(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", _router({
        "search/track": b'{"data": [{"title": "B", "artist": null, "album": {}}]}',
    }))
    assert await deezer.cover_for_track("A", "B") is None


@pytest.mark.asyncio
async def test_cover_for_track_network_error_propagates(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", _router({"search/track": OSError("boom")}))
    with pytest.raises(OSError):
        await deezer.cover_for_track("A", "B")
