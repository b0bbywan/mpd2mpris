"""Deezer cover-art fallback (no API key, stdlib only).

Broader catalogue than the Cover Art Archive, tried after ``musicbrainz``.
``cover_url`` returns an album cover URL (used as ``mpris:artUrl``, never
downloaded); ``resolve_album`` recovers (artist, album) from a web-radio
title MusicBrainz couldn't.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

from mpdris2 import _http
from mpdris2.translate import artist_matches, split_title

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.deezer.com/search/album"
_TRACK_SEARCH_URL = "https://api.deezer.com/search/track"
_COVER_FIELD = "cover_big"  # 500px, matching the other sources


async def cover_url(artist: str, album: str) -> str | None:
    """Album cover URL from Deezer, or ``None`` when no hit matches the artist."""
    logger.debug("deezer: cover for %r / %r", artist, album)
    return await asyncio.to_thread(_url_blocking, artist, album)


async def resolve_album(title: str) -> tuple[str, str] | None:
    """Recover (artist, album) from an ``Artist - Track`` title via a track
    search. ``None`` for an unparseable title or no confident match."""
    if not title:
        return None
    parsed = split_title(title)
    if parsed is None:
        return None
    return await asyncio.to_thread(_resolve_blocking, *parsed)


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


def _resolve_blocking(q_artist: str, q_track: str) -> tuple[str, str] | None:
    # Keep the top hit only when its artist matches, so a loose coincidence
    # never yields a wrong album.
    q = f'artist:"{q_artist}" track:"{q_track}"'
    url = f"{_TRACK_SEARCH_URL}?{urllib.parse.urlencode({'q': q, 'limit': 1})}"
    items = (json.loads(_http.get(url)).get("data")) or []
    if not items:
        logger.debug("deezer: no track for %r / %r", q_artist, q_track)
        return None
    top = items[0]
    artist = top.get("artist", {}).get("name") or ""
    album = top.get("album", {}).get("title") or ""
    if album and artist_matches(q_artist, artist):
        logger.debug("deezer: %r / %r -> %r / %r", q_artist, q_track, artist, album)
        return artist, album
    logger.debug("deezer: no confident match for %r / %r (closest: %r / %r)",
                 q_artist, q_track, artist, top.get("title"))
    return None
