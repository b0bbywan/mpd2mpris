"""iTunes Search cover-art fallback (no API key, stdlib only).

Broad catalogue, tried after ``musicbrainz``. ``cover_url`` returns an
album artwork URL (used as ``mpris:artUrl``, never downloaded).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

from mpdris2 import _http
from mpdris2.translate import artist_matches

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://itunes.apple.com/search"
# artworkUrl100 ends in ``100x100bb.jpg``; swap the size up for a usable cover.
_ART_SIZE = "600x600"


async def cover_url(artist: str, album: str) -> str | None:
    """Album artwork URL from iTunes, or ``None`` when no hit matches the artist."""
    logger.debug("itunes: cover for %r / %r", artist, album)
    return await asyncio.to_thread(_url_blocking, artist, album)


def _url_blocking(artist: str, album: str) -> str | None:
    params = {"term": f"{artist} {album}", "entity": "album", "limit": 1}
    url = f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    items = (json.loads(_http.get(url)).get("results")) or []
    if not items:
        logger.debug("itunes: no album for %r / %r", artist, album)
        return None
    top = items[0]
    if not artist_matches(artist, top.get("artistName", "")):
        logger.debug("itunes: artist mismatch for %r / %r", artist, album)
        return None
    art = top.get("artworkUrl100")
    if not art:
        logger.debug("itunes: no artwork url for %r / %r", artist, album)
        return None
    return str(art).replace("100x100", _ART_SIZE)
