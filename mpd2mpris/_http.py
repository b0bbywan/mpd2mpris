"""Shared stdlib HTTP for the no-auth cover-art fallbacks
(``deezer`` / ``itunes`` / ``mympd`` / ``radiobrowser``): a thin urllib
wrapper carrying mpd2mpris's User-Agent + timeout.

The loose artist matcher those modules use lives in ``translate``
(``artist_matches``). ``musicbrainz`` goes through ``musicbrainzngs`` and
keeps its own (accent-folding, fuzzy) matcher.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from mpd2mpris import APP, URL, __version__
from mpd2mpris.translate import artist_matches

logger = logging.getLogger(__name__)

_TIMEOUT = 10
_HEADERS = {"User-Agent": f"{APP}/{__version__} ({URL})"}


def get(url: str) -> bytes:
    """GET ``url`` and return the raw body; transport errors propagate."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (http(s) only)
        return bytes(resp.read())


def get_json(url: str) -> Any:
    """GET ``url`` and decode its JSON body."""
    return json.loads(get(url))


def is_image(url: str) -> bool:
    """HEAD ``url``; True if it serves an image. False on 404/410 or a
    non-image ``Content-Type``. Other errors propagate (caller retries).
    A missing ``Content-Type`` or a HEAD-refusing server gets a pass."""
    req = urllib.request.Request(url, headers=_HEADERS, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (http(s) only)
            ctype = resp.headers.get("Content-Type", "").lower()
            return not ctype or ctype.startswith("image/")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return False
        if e.code in (405, 501):  # HEAD unsupported
            return True
        raise


def post(url: str, body: bytes) -> bytes:
    """POST the JSON ``body`` to ``url`` and return the raw body."""
    headers = {**_HEADERS, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (http(s) only)
        return bytes(resp.read())


def search_cover(
    url: str,
    *,
    label: str,
    data_key: str,
    artist: str,
    artist_of: Callable[[dict[str, Any]], str],
    cover_of: Callable[[dict[str, Any]], str | None],
) -> str | None:
    """Shared skeleton for the no-auth JSON-search cover sources
    (``deezer`` / ``itunes``): GET ``url``, take the first item under
    ``data_key``, keep it only when ``artist_of(item)`` loosely matches
    ``artist`` (so a coincidental hit isn't served), and return
    ``cover_of(item)``. ``None`` — with a debug line naming the stage that
    bailed — on no items, artist mismatch, or no cover URL. Transport errors
    propagate so the caller skips caching a transient failure."""
    items = get_json(url).get(data_key) or []
    if not items:
        logger.debug("%s: no result for %r", label, artist)
        return None
    top = items[0]
    if not artist_matches(artist, artist_of(top)):
        logger.debug("%s: artist mismatch for %r", label, artist)
        return None
    cover = cover_of(top)
    if not cover:
        logger.debug("%s: no cover for %r", label, artist)
        return None
    return str(cover)
