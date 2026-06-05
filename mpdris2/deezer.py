"""Deezer cover-art fallback (no API key, stdlib only).

Broader catalogue than the Cover Art Archive, tried after ``musicbrainz``.
``cover_url`` covers a tagged (artist, album); ``cover_for_track`` covers a
web-radio (artist, track) via a track search. Both return a cover URL (used
as ``mpris:artUrl``, never downloaded).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

from mpdris2 import _http
from mpdris2.translate import artist_matches

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.deezer.com/search/album"
_TRACK_SEARCH_URL = "https://api.deezer.com/search/track"
_COVER_FIELD = "cover_big"  # 500px, matching the other sources


async def cover_url(artist: str, album: str) -> str | None:
    """Album cover URL from Deezer, or ``None`` when no hit matches the artist."""
    logger.debug("deezer: cover for %r / %r", artist, album)
    return await asyncio.to_thread(_url_blocking, artist, album)


async def cover_for_track(artist: str, track: str) -> str | None:
    """Album cover URL for a web-radio (artist, track), from a track search.
    ``None`` when no hit matches the artist."""
    logger.debug("deezer: cover for track %r / %r", artist, track)
    return await asyncio.to_thread(_cover_for_track_blocking, artist, track)


def _url_blocking(artist: str, album: str) -> str | None:
    q = f'artist:"{artist}" album:"{album}"'
    url = f"{_SEARCH_URL}?{urllib.parse.urlencode({'q': q, 'limit': 1})}"
    items = (json.loads(_http.get(url)).get("data")) or []
    if not items:
        logger.debug("deezer: no album for %r / %r", artist, album)
        return None
    top = items[0]
    if not artist_matches(artist, top.get("artist", {}).get("name", "")):
        logger.debug("deezer: artist mismatch for %r / %r", artist, album)
        return None
    cover = top.get(_COVER_FIELD) or top.get("cover_xl")
    if not cover:
        logger.debug("deezer: no cover url for %r / %r", artist, album)
        return None
    return str(cover)


def _cover_for_track_blocking(q_artist: str, q_track: str) -> str | None:
    # The track hit already carries its album's cover, so no second lookup.
    # Keep it only when the artist matches, so a coincidence isn't served.
    q = f'artist:"{q_artist}" track:"{q_track}"'
    url = f"{_TRACK_SEARCH_URL}?{urllib.parse.urlencode({'q': q, 'limit': 1})}"
    items = (json.loads(_http.get(url)).get("data")) or []
    if not items:
        logger.debug("deezer: no track for %r / %r", q_artist, q_track)
        return None
    top = items[0]
    if not artist_matches(q_artist, top.get("artist", {}).get("name", "")):
        logger.debug("deezer: artist mismatch for %r / %r", q_artist, q_track)
        return None
    album = top.get("album") or {}
    cover = album.get(_COVER_FIELD) or album.get("cover_xl")
    if not cover:
        logger.debug("deezer: no cover for %r / %r", q_artist, q_track)
        return None
    logger.debug("deezer: %r / %r -> %s", q_artist, q_track, cover)
    return str(cover)
