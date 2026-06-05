"""iTunes Search cover-art fallback (no API key, stdlib only).

Broad catalogue, tried after ``musicbrainz``. ``cover_url`` covers a tagged
(artist, album); ``cover_for_track`` covers a web-radio (artist, track) via
a song search. Both return an artwork URL (used as ``mpris:artUrl``, never
downloaded).
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any

from mpdris2 import _http

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://itunes.apple.com/search"
# artworkUrl100 ends in ``100x100bb.jpg``; swap the size up for a usable cover.
_ART_SIZE = "600x600"


async def cover_url(artist: str, album: str) -> str | None:
    """Album artwork URL from iTunes, or ``None`` when no hit matches the artist."""
    logger.debug("itunes: cover for %r / %r", artist, album)
    return await asyncio.to_thread(_url_blocking, "album", f"{artist} {album}", artist)


async def cover_for_track(artist: str, track: str) -> str | None:
    """Artwork URL for a web-radio (artist, track), from a song search.
    ``None`` when no hit matches the artist."""
    logger.debug("itunes: cover for track %r / %r", artist, track)
    return await asyncio.to_thread(_url_blocking, "song", f"{artist} {track}", artist)


def _upscale(top: dict[str, Any]) -> str | None:
    art = top.get("artworkUrl100")
    if not art:
        return None
    # Swap only the trailing size token (``…/100x100bb.jpg``); replace the
    # last occurrence so a ``100x100`` elsewhere in the CDN path is left alone.
    head, sep, tail = str(art).rpartition("100x100")
    return head + _ART_SIZE + tail if sep else str(art)


def _url_blocking(entity: str, term: str, artist: str) -> str | None:
    url = f"{_SEARCH_URL}?{urllib.parse.urlencode({'term': term, 'entity': entity, 'limit': 1})}"
    return _http.search_cover(
        url, label="itunes", data_key="results", artist=artist,
        artist_of=lambda top: str(top.get("artistName", "")),
        cover_of=_upscale,
    )
