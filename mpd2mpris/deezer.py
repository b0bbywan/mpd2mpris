"""Deezer cover-art fallback (no API key, stdlib only).

Broader catalogue than the Cover Art Archive, tried after ``musicbrainz``.
``cover_url`` covers a tagged (artist, album); ``cover_for_track`` covers a
web-radio (artist, track) via a track search. Both return a cover URL (used
as ``mpris:artUrl``, never downloaded).
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any

from mpd2mpris import _http

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


def _artist_name(top: dict[str, Any]) -> str:
    return str((top.get("artist") or {}).get("name", ""))


def _url_blocking(artist: str, album: str) -> str | None:
    q = f'artist:"{artist}" album:"{album}"'
    url = f"{_SEARCH_URL}?{urllib.parse.urlencode({'q': q, 'limit': 1})}"
    return _http.search_cover(
        url, label="deezer", data_key="data", artist=artist,
        artist_of=_artist_name,
        cover_of=lambda top: top.get(_COVER_FIELD) or top.get("cover_xl"),
    )


def _cover_for_track_blocking(q_artist: str, q_track: str) -> str | None:
    # The track hit already carries its album's cover, so no second lookup.
    q = f'artist:"{q_artist}" track:"{q_track}"'
    url = f"{_TRACK_SEARCH_URL}?{urllib.parse.urlencode({'q': q, 'limit': 1})}"

    def _cover(top: dict[str, Any]) -> str | None:
        album = top.get("album") or {}
        return str(album.get(_COVER_FIELD) or album.get("cover_xl") or "") or None

    return _http.search_cover(
        url, label="deezer", data_key="data", artist=q_artist,
        artist_of=_artist_name,
        cover_of=_cover,
    )
