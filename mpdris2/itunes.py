"""iTunes Search cover-art fallback (no API key, stdlib only).

Broad catalogue, tried after ``musicbrainz``. ``cover_url`` covers a tagged
(artist, album); ``cover_for_track`` covers a web-radio (artist, track) via
a song search. Both return an artwork URL (used as ``mpris:artUrl``, never
downloaded).
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
    return await asyncio.to_thread(_url_blocking, "album", f"{artist} {album}", artist)


async def cover_for_track(artist: str, track: str) -> str | None:
    """Artwork URL for a web-radio (artist, track), from a song search.
    ``None`` when no hit matches the artist."""
    logger.debug("itunes: cover for track %r / %r", artist, track)
    return await asyncio.to_thread(_url_blocking, "song", f"{artist} {track}", artist)


def _url_blocking(entity: str, term: str, artist: str) -> str | None:
    url = f"{_SEARCH_URL}?{urllib.parse.urlencode({'term': term, 'entity': entity, 'limit': 1})}"
    items = (json.loads(_http.get(url)).get("results")) or []
    if not items:
        logger.debug("itunes: no %s for %r", entity, term)
        return None
    top = items[0]
    if not artist_matches(artist, top.get("artistName", "")):
        logger.debug("itunes: artist mismatch for %r", term)
        return None
    art = top.get("artworkUrl100")
    if not art:
        logger.debug("itunes: no artwork url for %r", term)
        return None
    cover = str(art).replace("100x100", _ART_SIZE)
    logger.debug("itunes: %r -> %s", term, cover)
    return cover
