"""Radio-browser station-favicon fallback (no API key, stdlib only).

``station_icon`` looks a stream URL up on the Community Radio Browser and
returns the station's favicon URL (used as ``mpris:artUrl``, never
downloaded).
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse

from mpd2mpris import _http

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
    stations = _http.get_json(url)
    if not isinstance(stations, list):  # error/HTML payload, not the station array
        logger.debug("radiobrowser: unexpected response for %s", stream_url)
        return None
    error: Exception | None = None
    for station in stations:
        favicon = str(station.get("favicon") or "").strip()
        if not favicon.startswith(("http://", "https://")):
            continue
        try:
            ok = _http.is_image(favicon)
        except Exception as e:
            # A transient HEAD failure on one favicon shouldn't abandon the
            # rest — remember it and keep scanning later stations.
            logger.debug("radiobrowser: %r favicon %s check failed: %r", station.get("name"), favicon, e)
            error = error or e
            continue
        if not ok:
            logger.debug("radiobrowser: %r favicon %s dead/not an image, skipping",
                         station.get("name"), favicon)
            continue
        logger.debug("radiobrowser: %r favicon %s", station.get("name"), favicon)
        return favicon
    if error is not None:
        # No usable favicon found AND a transient error occurred — propagate so
        # the caller skips caching and retries, rather than caching a false miss.
        raise error
    logger.debug("radiobrowser: no station/favicon for %s", stream_url)
    return None
