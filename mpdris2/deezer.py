"""Deezer cover-art fallback (no API key, stdlib only).

Cover Art Archive is sparse for a lot of content; Deezer's public search
API has much broader coverage and needs no authentication. Used as a
fallback after ``mpdris2.musicbrainz`` when CAA has no image. Two entry
points, both async (the urllib calls run in a worker thread):

* ``fetch_cover``   — download an album's cover bytes.
* ``resolve_album`` — recover (artist, album) from a free-form web-radio
  title, for the cases MusicBrainz can't resolve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
import urllib.request

from mpdris2 import __version__
from mpdris2.translate import split_title

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.deezer.com/search/album"
_TRACK_SEARCH_URL = "https://api.deezer.com/search/track"
# cover sizes: cover_small/medium/big(500)/xl(1000). 500 matches the rest.
_COVER_FIELD = "cover_big"
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
    """Download an album's cover from Deezer, or ``None`` when nothing
    matches the artist."""
    logger.debug("deezer: cover for %r / %r", artist, album)
    return await asyncio.to_thread(_fetch_blocking, artist, album)


async def resolve_album(title: str) -> tuple[str, str] | None:
    """Recover (artist, album) from an ``Artist - Track`` web-radio title
    via a track search, validating the artist actually matches. ``None``
    for an unparseable title or no confident match. Complements
    ``musicbrainz.resolve_album`` — Deezer covers content MB doesn't."""
    if not title:
        return None
    parsed = split_title(title)
    if parsed is None:
        return None
    return await asyncio.to_thread(_resolve_blocking, *parsed)


def _fetch_blocking(artist: str, album: str) -> bytes | None:
    try:
        q = f'artist:"{artist}" album:"{album}"'
        url = f"{_SEARCH_URL}?{urllib.parse.urlencode({'q': q, 'limit': 1})}"
        items = (json.loads(_get(url)).get("data")) or []
        if not items:
            logger.debug("deezer: no album for %r / %r", artist, album)
            return None
        top = items[0]
        if not _artist_matches(artist, top.get("artist", {}).get("name", "")):
            logger.debug("deezer: artist mismatch for %r / %r", artist, album)
            return None
        cover_url = top.get(_COVER_FIELD) or top.get("cover_xl")
        if not cover_url:
            logger.debug("deezer: no cover url for %r / %r", artist, album)
            return None
        return _get(cover_url)
    except Exception as e:
        logger.debug("deezer: lookup for %r / %r failed: %r", artist, album, e)
        return None


def _resolve_blocking(q_artist: str, q_track: str) -> tuple[str, str] | None:
    # Fielded track search; keep the top hit only when its artist matches,
    # so a loose coincidence never yields a wrong album/cover.
    try:
        q = f'artist:"{q_artist}" track:"{q_track}"'
        url = f"{_TRACK_SEARCH_URL}?{urllib.parse.urlencode({'q': q, 'limit': 1})}"
        items = (json.loads(_get(url)).get("data")) or []
        if not items:
            logger.debug("deezer: no track for %r / %r", q_artist, q_track)
            return None
        top = items[0]
        artist = top.get("artist", {}).get("name") or ""
        album = top.get("album", {}).get("title") or ""
        if album and _artist_matches(q_artist, artist):
            logger.debug("deezer: %r / %r -> %r / %r", q_artist, q_track, artist, album)
            return artist, album
        logger.debug("deezer: no confident match for %r / %r (closest: %r / %r)",
                     q_artist, q_track, artist, top.get("title"))
        return None
    except Exception as e:
        logger.debug("deezer: resolve for %r / %r failed: %r", q_artist, q_track, e)
        return None
