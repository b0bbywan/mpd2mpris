"""Unit tests for the myMPD WebradioDB cover fallback. ``_post`` (the
only network touch-point) is monkeypatched; nothing hits the network."""

from __future__ import annotations

import json

import pytest

from mpd2mpris import mympd

_IMG = "https://jcorporation.github.io/webradiodb/db/pics/stream.webp"


@pytest.mark.asyncio
async def test_cover_url_returns_image(monkeypatch) -> None:
    calls: list[tuple[str, bytes]] = []

    def _post(url: str, body: bytes) -> bytes:
        calls.append((url, body))
        return json.dumps({"result": {"Name": "X", "Image": _IMG}}).encode()

    monkeypatch.setattr("mpd2mpris._http.post", _post)
    assert await mympd.cover_url("http://host:8080", "http://stream") == _IMG
    # endpoint built from the base URL; the stream URI is in the request body.
    url, body = calls[0]
    assert url == "http://host:8080/api/default"
    assert json.loads(body)["params"]["uri"] == "http://stream"


@pytest.mark.asyncio
async def test_cover_url_strips_trailing_slash(monkeypatch) -> None:
    seen: list[str] = []

    def _post(url: str, body: bytes) -> bytes:
        seen.append(url)
        return b'{"result": {"Image": "' + _IMG.encode() + b'"}}'

    monkeypatch.setattr("mpd2mpris._http.post", _post)
    await mympd.cover_url("http://host:8080/", "http://stream")
    assert seen == ["http://host:8080/api/default"]


@pytest.mark.asyncio
async def test_cover_url_entry_not_found(monkeypatch) -> None:
    # A miss is a JSON-RPC error object with no "result".
    err = json.dumps({"error": {"message": "Webradio entry not found"}}).encode()
    monkeypatch.setattr("mpd2mpris._http.post", lambda url, body: err)
    assert await mympd.cover_url("http://host:8080", "http://stream") is None


@pytest.mark.asyncio
async def test_cover_url_null_result(monkeypatch) -> None:
    # Some responses carry an explicit ``"result": null`` — a clean miss,
    # not an AttributeError.
    monkeypatch.setattr("mpd2mpris._http.post", lambda url, body: b'{"result": null}')
    assert await mympd.cover_url("http://host:8080", "http://stream") is None


@pytest.mark.asyncio
async def test_cover_url_no_image_field(monkeypatch) -> None:
    monkeypatch.setattr("mpd2mpris._http.post", lambda url, body: b'{"result": {"Name": "X"}}')
    assert await mympd.cover_url("http://host:8080", "http://stream") is None


@pytest.mark.asyncio
async def test_cover_url_network_error_propagates(monkeypatch) -> None:
    # A transient error must propagate (not become None) so cover.py can
    # skip caching and retry later.
    def _boom(url: str, body: bytes) -> bytes:
        raise OSError("boom")

    monkeypatch.setattr("mpd2mpris._http.post", _boom)
    with pytest.raises(OSError):
        await mympd.cover_url("http://host:8080", "http://stream")
