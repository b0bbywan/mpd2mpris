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
        favicon = station.get("favicon")
        if favicon:
            logger.debug("radiobrowser: %r favicon %s", station.get("name"), favicon)
            return str(favicon)
    logger.debug("radiobrowser: no station/favicon for %s", stream_url)
    return None
