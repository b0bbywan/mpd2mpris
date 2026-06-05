"""Unit tests for the radio-browser station-favicon fallback. ``_get``
(the only network touch-point) is monkeypatched; nothing hits the network."""

from __future__ import annotations

import json

import pytest

from mpdris2 import radiobrowser


@pytest.mark.asyncio
async def test_station_icon_returns_favicon_url(monkeypatch) -> None:
    calls: list[str] = []
    payload = json.dumps([{"name": "X", "favicon": "https://x/favicon.ico"}]).encode()

    def _get(url: str) -> bytes:
        calls.append(url)
        return payload

    monkeypatch.setattr(radiobrowser, "_get", _get)
    assert await radiobrowser.station_icon("http://stream") == "https://x/favicon.ico"
    assert len(calls) == 1  # only the JSON lookup — favicon is not downloaded


@pytest.mark.asyncio
async def test_station_icon_no_station(monkeypatch) -> None:
    monkeypatch.setattr(radiobrowser, "_get", lambda url: b"[]")
    assert await radiobrowser.station_icon("http://stream") is None


@pytest.mark.asyncio
async def test_station_icon_no_favicon(monkeypatch) -> None:
    monkeypatch.setattr(radiobrowser, "_get", lambda url: json.dumps([{"name": "X", "favicon": ""}]).encode())
    assert await radiobrowser.station_icon("http://stream") is None


@pytest.mark.asyncio
async def test_station_icon_network_error(monkeypatch) -> None:
    def _boom(url: str) -> bytes:
        raise OSError("boom")

    monkeypatch.setattr(radiobrowser, "_get", _boom)
    assert await radiobrowser.station_icon("http://stream") is None
