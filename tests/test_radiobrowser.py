"""Unit tests for the radio-browser station-favicon fallback. ``_get``
(the only network touch-point) is monkeypatched; nothing hits the network."""

from __future__ import annotations

import json

import pytest

from mpdris2 import radiobrowser


@pytest.fixture(autouse=True)
def _favicons_alive(monkeypatch):
    """Default: every favicon HEAD-checks as live. Tests that need a dead
    link override ``_http.url_exists`` themselves."""
    monkeypatch.setattr("mpdris2._http.url_exists", lambda url: True)


@pytest.mark.asyncio
async def test_station_icon_returns_favicon_url(monkeypatch) -> None:
    calls: list[str] = []
    payload = json.dumps([{"name": "X", "favicon": "https://x/favicon.ico"}]).encode()

    def _get(url: str) -> bytes:
        calls.append(url)
        return payload

    monkeypatch.setattr("mpdris2._http.get", _get)
    assert await radiobrowser.station_icon("http://stream") == "https://x/favicon.ico"
    assert len(calls) == 1  # only the JSON lookup — favicon is not downloaded


@pytest.mark.asyncio
async def test_station_icon_no_station(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", lambda url: b"[]")
    assert await radiobrowser.station_icon("http://stream") is None


@pytest.mark.asyncio
async def test_station_icon_no_favicon(monkeypatch) -> None:
    monkeypatch.setattr("mpdris2._http.get", lambda url: json.dumps([{"name": "X", "favicon": ""}]).encode())
    assert await radiobrowser.station_icon("http://stream") is None


@pytest.mark.asyncio
async def test_station_icon_literal_null_string(monkeypatch) -> None:
    # The API hands back the literal string "null" (not JSON null) for
    # favicon-less stations — it must not be served as a URL.
    monkeypatch.setattr("mpdris2._http.get", lambda url: json.dumps([{"name": "X", "favicon": "null"}]).encode())
    assert await radiobrowser.station_icon("http://stream") is None


@pytest.mark.asyncio
async def test_station_icon_skips_dead_favicon(monkeypatch) -> None:
    # First station's favicon 404s; fall through to the next live one.
    payload = json.dumps([
        {"name": "Dead", "favicon": "https://dead/favicon.ico"},
        {"name": "Live", "favicon": "https://live/favicon.ico"},
    ]).encode()
    monkeypatch.setattr("mpdris2._http.get", lambda url: payload)
    monkeypatch.setattr("mpdris2._http.url_exists", lambda url: url != "https://dead/favicon.ico")
    assert await radiobrowser.station_icon("http://stream") == "https://live/favicon.ico"


@pytest.mark.asyncio
async def test_station_icon_all_favicons_dead(monkeypatch) -> None:
    payload = json.dumps([{"name": "Dead", "favicon": "https://dead/favicon.ico"}]).encode()
    monkeypatch.setattr("mpdris2._http.get", lambda url: payload)
    monkeypatch.setattr("mpdris2._http.url_exists", lambda url: False)
    assert await radiobrowser.station_icon("http://stream") is None


@pytest.mark.asyncio
async def test_station_icon_network_error_propagates(monkeypatch) -> None:
    # A transient error must propagate (not become None) so cover.py can
    # skip caching and retry later.
    def _boom(url: str) -> bytes:
        raise OSError("boom")

    monkeypatch.setattr("mpdris2._http.get", _boom)
    with pytest.raises(OSError):
        await radiobrowser.station_icon("http://stream")
