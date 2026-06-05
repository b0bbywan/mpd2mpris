"""Radio-browser station-favicon fallback (no API key, stdlib only).

``station_icon`` looks a stream URL up on the Community Radio Browser and
returns the station's favicon URL (used as ``mpris:artUrl``, never
downloaded).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

from mpdris2 import _http

logger = logging.getLogger(__name__)

_API = "https://all.api.radio-browser.info/json/stations/byurl"


async def station_icon(stream_url: str) -> str | None:
    """Return the favicon URL of the station serving ``stream_url``, or
    ``None`` when it's unknown or has no favicon. Raises on a network/
    transient error so the caller can skip caching and retry."""
    logger.debug("radiobrowser: lookup %s", stream_url)
    return await asyncio.to_thread(_lookup_blocking, stream_url)


def _lookup_blocking(stream_url: str) -> str | None:
    url = f"{_API}?{urllib.parse.urlencode({'url': stream_url})}"
    for station in json.loads(_http.get(url)):
        favicon = str(station.get("favicon") or "").strip()
        if not favicon.startswith(("http://", "https://")):
            continue
        if not _http.is_image(favicon):
            logger.debug("radiobrowser: %r favicon %s dead/not an image, skipping",
                         station.get("name"), favicon)
            continue
        logger.debug("radiobrowser: %r favicon %s", station.get("name"), favicon)
        return favicon
    logger.debug("radiobrowser: no station/favicon for %s", stream_url)
    return None
