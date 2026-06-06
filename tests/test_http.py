"""Unit tests for the shared stdlib HTTP helpers. ``urllib.request.urlopen``
is monkeypatched throughout; nothing hits the network.

Covers the bits the per-source tests only exercise indirectly: the
``is_image`` HEAD status-code matrix and the ``search_cover`` skeleton that
``deezer``/``itunes`` delegate to.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from mpdris2 import _http


class _Resp:
    """Minimal stand-in for a urlopen context manager."""

    def __init__(self, *, body: bytes = b"", headers: dict | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _patch_urlopen(monkeypatch, *, body: bytes = b"", headers: dict | None = None,
                   raise_code: int | None = None) -> None:
    def _open(req, timeout=None):
        if raise_code is not None:
            raise urllib.error.HTTPError(req.full_url, raise_code, "err", {}, None)  # type: ignore[arg-type]
        return _Resp(body=body, headers=headers)
    monkeypatch.setattr(urllib.request, "urlopen", _open)


# --- is_image: the HEAD status/content-type matrix -----------------------


def test_is_image_true_for_image_content_type(monkeypatch) -> None:
    _patch_urlopen(monkeypatch, headers={"Content-Type": "image/png"})
    assert _http.is_image("https://x/cover.png") is True


def test_is_image_false_for_non_image_content_type(monkeypatch) -> None:
    # Some favicon entries point at the station homepage (text/html).
    _patch_urlopen(monkeypatch, headers={"Content-Type": "text/html; charset=utf-8"})
    assert _http.is_image("https://x/") is False


def test_is_image_true_when_content_type_missing(monkeypatch) -> None:
    # A HEAD-refusing server or a missing Content-Type gets the benefit of the
    # doubt rather than dropping a possibly-valid cover.
    _patch_urlopen(monkeypatch, headers={})
    assert _http.is_image("https://x/cover") is True


@pytest.mark.parametrize("code", [404, 410])
def test_is_image_false_on_gone(monkeypatch, code: int) -> None:
    _patch_urlopen(monkeypatch, raise_code=code)
    assert _http.is_image("https://x/dead") is False


@pytest.mark.parametrize("code", [405, 501])
def test_is_image_true_when_head_unsupported(monkeypatch, code: int) -> None:
    # The server refuses HEAD, not the resource — assume the image exists.
    _patch_urlopen(monkeypatch, raise_code=code)
    assert _http.is_image("https://x/cover.jpg") is True


def test_is_image_propagates_other_http_errors(monkeypatch) -> None:
    # A 500/429 is transient — propagate so the caller skips caching a miss.
    _patch_urlopen(monkeypatch, raise_code=500)
    with pytest.raises(urllib.error.HTTPError):
        _http.is_image("https://x/cover.jpg")


# --- get_json / post: trivial transport wrappers -------------------------


def test_get_json_decodes_body(monkeypatch) -> None:
    _patch_urlopen(monkeypatch, body=b'{"a": 1, "b": [2, 3]}')
    assert _http.get_json("https://x/api") == {"a": 1, "b": [2, 3]}


def test_post_returns_raw_body(monkeypatch) -> None:
    seen: dict = {}

    def _open(req, timeout=None):
        seen["data"] = req.data
        seen["method"] = req.get_method()
        return _Resp(body=b"pong")

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    assert _http.post("https://x/api", b"ping") == b"pong"
    assert seen == {"data": b"ping", "method": "POST"}


# --- search_cover: the shared deezer/itunes skeleton ---------------------


def _payload(items: list) -> bytes:
    return json.dumps({"data": items}).encode()


def _call(monkeypatch, raw: bytes, *, artist: str = "A"):
    monkeypatch.setattr(_http, "get", lambda url: raw)
    return _http.search_cover(
        "https://x/search", label="t", data_key="data", artist=artist,
        artist_of=lambda item: item.get("who", ""),
        cover_of=lambda item: item.get("img"),
    )


def test_search_cover_returns_first_hit(monkeypatch) -> None:
    out = _call(monkeypatch, _payload([{"who": "A", "img": "https://cdn/c.jpg"}]))
    assert out == "https://cdn/c.jpg"


def test_search_cover_empty_items_returns_none(monkeypatch) -> None:
    assert _call(monkeypatch, _payload([])) is None


def test_search_cover_missing_data_key_returns_none(monkeypatch) -> None:
    # An error envelope with no ``data`` key must be a clean miss.
    assert _call(monkeypatch, b'{"error": "rate limited"}') is None


def test_search_cover_null_data_key_returns_none(monkeypatch) -> None:
    assert _call(monkeypatch, b'{"data": null}') is None


def test_search_cover_artist_mismatch_returns_none(monkeypatch) -> None:
    out = _call(monkeypatch, _payload([{"who": "Someone Else", "img": "https://cdn/c.jpg"}]))
    assert out is None


def test_search_cover_missing_cover_returns_none(monkeypatch) -> None:
    # Artist matches but the item carries no cover URL.
    out = _call(monkeypatch, _payload([{"who": "A"}]))
    assert out is None


def test_search_cover_considers_only_first_item(monkeypatch) -> None:
    # The search is asked for limit=1; search_cover commits to items[0]. A
    # mismatching first hit is a miss even if a later item would have matched.
    out = _call(monkeypatch, _payload([
        {"who": "Wrong", "img": "https://cdn/wrong.jpg"},
        {"who": "A", "img": "https://cdn/right.jpg"},
    ]))
    assert out is None


def test_search_cover_propagates_transport_error(monkeypatch) -> None:
    def _boom(url: str) -> bytes:
        raise OSError("boom")

    monkeypatch.setattr(_http, "get", _boom)
    with pytest.raises(OSError):
        _http.search_cover(
            "https://x/search", label="t", data_key="data", artist="A",
            artist_of=lambda item: "A", cover_of=lambda item: "x",
        )
