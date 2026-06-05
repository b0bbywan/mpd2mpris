"""Radio-browser station-favicon fallback (no API key, stdlib only).

Last resort for a web-radio stream whose current track has no album art:
look the stream URL up on the Community Radio Browser and return the
station's favicon *URL* — MPRIS clients fetch ``mpris:artUrl`` themselves,
so there's nothing to download or cache here. One entry point,
``station_icon``; async (the urllib call runs in a worker thread).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

from mpdris2 import _http

logger = logging.getLogger(__name__)

# Round-robin DNS entry point across the community mirrors.
_API = "https://all.api.radio-browser.info/json/stations/byurl"


async def station_icon(stream_url: str) -> str | None:
    """Return the favicon URL of the station serving ``stream_url``, or
    ``None`` when it's unknown or has no favicon. Raises on a network/
    transient error so the caller can skip caching and retry."""
    logger.debug("radiobrowser: lookup %s", stream_url)
    return await asyncio.to_thread(_lookup_blocking, stream_url)


def _lookup_blocking(stream_url: str) -> str | None:
    # A clean miss returns None; a network/transient error propagates so the
    # caller can skip caching and retry later.
    url = f"{_API}?{urllib.parse.urlencode({'url': stream_url})}"
    for station in json.loads(_http.get(url)):
        # The API hands back the literal string "null" (not JSON null) for
        # stations with no favicon — reject anything that isn't a real
        # http(s) URL so it can't win over a later fallback.
        favicon = str(station.get("favicon") or "").strip()
        if not favicon.startswith(("http://", "https://")):
            continue
        # The DB outlives the favicons it points at — skip dead links so a
        # 404 doesn't shadow a working myMPD/WebradioDB cover (step 7).
        if not _http.url_exists(favicon):
            logger.debug("radiobrowser: %r favicon %s is dead, skipping", station.get("name"), favicon)
            continue
        logger.debug("radiobrowser: %r favicon %s", station.get("name"), favicon)
        return favicon
    logger.debug("radiobrowser: no station/favicon for %s", stream_url)
    return None
