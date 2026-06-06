"""myMPD WebradioDB cover fallback (no API key, stdlib only).

When a web-radio stream has no album art and the radio-browser favicon
lookup also came up empty, ask a configured myMPD instance's WebradioDB
for the station entry: ``MYMPD_API_WEBRADIODB_RADIO_GET_BY_URI`` returns
a curated ``Image`` *URL* (hosted on webradiodb), served verbatim as
``mpris:artUrl`` — nothing to download or cache here. Disabled unless
``[Cover] mympd_uri`` points at a reachable myMPD. One entry point,
``cover_url``; async (the urllib call runs in a worker thread).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from mpdris2 import __version__

logger = logging.getLogger(__name__)

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": f"mpDris2/{__version__} (https://github.com/b0bbywan/mpDris2)",
}
_TIMEOUT = 10
_METHOD = "MYMPD_API_WEBRADIODB_RADIO_GET_BY_URI"


def _post(url: str, body: bytes) -> bytes:
    req = urllib.request.Request(url, data=body, headers=_HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
        return bytes(resp.read())


async def cover_url(base_url: str, stream_url: str) -> str | None:
    """Return the WebradioDB cover URL the myMPD at ``base_url`` has for
    ``stream_url``, or ``None`` when it's unknown or unreachable."""
    logger.debug("mympd: lookup %s via %s", stream_url, base_url)
    return await asyncio.to_thread(_lookup_blocking, base_url, stream_url)


def _lookup_blocking(base_url: str, stream_url: str) -> str | None:
    try:
        endpoint = base_url.rstrip("/") + "/api/default"
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 0,
            "method": _METHOD,
            "params": {"uri": stream_url},
        }).encode()
        # Miss → JSON-RPC "error" object (no "result"); .get chain yields None.
        image = json.loads(_post(endpoint, body)).get("result", {}).get("Image")
        if image:
            logger.debug("mympd: %s -> %s", stream_url, image)
            return str(image)
        logger.debug("mympd: no entry/image for %s", stream_url)
        return None
    except Exception as e:
        logger.debug("mympd: lookup for %s failed: %r", stream_url, e)
        return None
