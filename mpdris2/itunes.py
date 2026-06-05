"""iTunes Search cover-art fallback (no API key, stdlib only).

Cover Art Archive is sparse for a lot of content; Apple's iTunes Search
API has broad coverage and needs no authentication. Used as a fallback
after ``mpdris2.musicbrainz``. One entry point, ``fetch_cover``; async
(the urllib calls run in a worker thread).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
import urllib.request

from mpdris2 import __version__

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://itunes.apple.com/search"
# artworkUrl100 ends in ``100x100bb.jpg``; swap the size up for a usable cover.
_ART_SIZE = "600x600"
_HEADERS = {"User-Agent": f"mpDris2/{__version__} (https://github.com/b0bbywan/mpDris2)"}
_TIMEOUT = 10
_NORM = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    return _NORM.sub(" ", s.lower()).strip()


def _artist_matches(query: str, candidate: str) -> bool:
    q, c = _norm(query), _norm(candidate)
    return bool(q) and bool(c) and (q == c or q in c or c in q)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (https only)
        return bytes(resp.read())


async def fetch_cover(artist: str, album: str) -> bytes | None:
    """Download an album's cover from iTunes, or ``None`` when nothing
    matches the artist."""
    logger.debug("itunes: cover for %r / %r", artist, album)
    return await asyncio.to_thread(_fetch_blocking, artist, album)


def _fetch_blocking(artist: str, album: str) -> bytes | None:
    try:
        params = {"term": f"{artist} {album}", "entity": "album", "limit": 1}
        url = f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"
        items = (json.loads(_get(url)).get("results")) or []
        if not items:
            logger.debug("itunes: no album for %r / %r", artist, album)
            return None
        top = items[0]
        if not _artist_matches(artist, top.get("artistName", "")):
            logger.debug("itunes: artist mismatch for %r / %r", artist, album)
            return None
        art = top.get("artworkUrl100")
        if not art:
            logger.debug("itunes: no artwork url for %r / %r", artist, album)
            return None
        return _get(art.replace("100x100", _ART_SIZE))
    except Exception as e:
        logger.debug("itunes: lookup for %r / %r failed: %r", artist, album, e)
        return None
