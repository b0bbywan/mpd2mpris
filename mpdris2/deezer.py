"""Deezer cover-art fallback (no API key, stdlib only).

Cover Art Archive is sparse for a lot of content; Deezer's public search
API has much broader coverage and needs no authentication. Used as a
fallback after ``mpdris2.musicbrainz`` when CAA has no image. Two entry
points, both async (the urllib calls run in a worker thread):

* ``cover_url``     — an album's cover **URL** (used as ``mpris:artUrl``;
  the image isn't downloaded, the MPRIS client fetches it).
* ``resolve_album`` — recover (artist, album) from a free-form web-radio
  title, for the cases MusicBrainz can't resolve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

from mpdris2 import _http
from mpdris2.translate import split_title

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.deezer.com/search/album"
_TRACK_SEARCH_URL = "https://api.deezer.com/search/track"
# cover sizes: cover_small/medium/big(500)/xl(1000). 500 matches the rest.
_COVER_FIELD = "cover_big"


async def cover_url(artist: str, album: str) -> str | None:
    """Cover URL for an album from Deezer, or ``None`` when nothing
    matches the artist. The search hit confirms the cover exists."""
    logger.debug("deezer: cover for %r / %r", artist, album)
    return await asyncio.to_thread(_url_blocking, artist, album)


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


def _url_blocking(artist: str, album: str) -> str | None:
    # A clean miss returns None; a network/transient error propagates so the
    # caller can skip caching and retry later.
    q = f'artist:"{artist}" album:"{album}"'
    url = f"{_SEARCH_URL}?{urllib.parse.urlencode({'q': q, 'limit': 1})}"
    items = (json.loads(_http.get(url)).get("data")) or []
    if not items:
        logger.debug("deezer: no album for %r / %r", artist, album)
        return None
    top = items[0]
    if not _http.artist_matches(artist, top.get("artist", {}).get("name", "")):
        logger.debug("deezer: artist mismatch for %r / %r", artist, album)
        return None
    cover = top.get(_COVER_FIELD) or top.get("cover_xl")
    if not cover:
        logger.debug("deezer: no cover url for %r / %r", artist, album)
        return None
    return str(cover)


def _resolve_blocking(q_artist: str, q_track: str) -> tuple[str, str] | None:
    # Fielded track search; keep the top hit only when its artist matches,
    # so a loose coincidence never yields a wrong album/cover. A clean miss
    # returns None; a network/transient error propagates.
    q = f'artist:"{q_artist}" track:"{q_track}"'
    url = f"{_TRACK_SEARCH_URL}?{urllib.parse.urlencode({'q': q, 'limit': 1})}"
    items = (json.loads(_http.get(url)).get("data")) or []
    if not items:
        logger.debug("deezer: no track for %r / %r", q_artist, q_track)
        return None
    top = items[0]
    artist = top.get("artist", {}).get("name") or ""
    album = top.get("album", {}).get("title") or ""
    if album and _http.artist_matches(q_artist, artist):
        logger.debug("deezer: %r / %r -> %r / %r", q_artist, q_track, artist, album)
        return artist, album
    logger.debug("deezer: no confident match for %r / %r (closest: %r / %r)",
                 q_artist, q_track, artist, top.get("title"))
    return None
