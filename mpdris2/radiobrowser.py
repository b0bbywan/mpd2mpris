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
import urllib.request

from mpdris2 import __version__

logger = logging.getLogger(__name__)

# Round-robin DNS entry point across the community mirrors.
_API = "https://all.api.radio-browser.info/json/stations/byurl"
_HEADERS = {"User-Agent": f"mpDris2/{__version__} (https://github.com/b0bbywan/mpDris2)"}
_TIMEOUT = 10


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (https only)
        return bytes(resp.read())


async def station_icon(stream_url: str) -> str | None:
    """Return the favicon URL of the station serving ``stream_url``, or
    ``None`` when it's unknown or has no favicon."""
    logger.debug("radiobrowser: lookup %s", stream_url)
    return await asyncio.to_thread(_lookup_blocking, stream_url)


def _lookup_blocking(stream_url: str) -> str | None:
    try:
        url = f"{_API}?{urllib.parse.urlencode({'url': stream_url})}"
        for station in json.loads(_get(url)):
            favicon = station.get("favicon")
            if favicon:
                logger.debug("radiobrowser: %r favicon %s", station.get("name"), favicon)
                return str(favicon)
        logger.debug("radiobrowser: no station/favicon for %s", stream_url)
        return None
    except Exception as e:
        logger.debug("radiobrowser: lookup for %s failed: %r", stream_url, e)
        return None
