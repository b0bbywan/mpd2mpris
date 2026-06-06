"""myMPD WebradioDB cover fallback (no API key, stdlib only).

When a web-radio stream has no album art and the radio-browser favicon
lookup also came up empty, ask a configured myMPD instance's WebradioDB
for the station entry: ``MYMPD_API_WEBRADIODB_RADIO_GET_BY_URI`` returns
a curated ``Image`` *URL* (hosted on webradiodb) used as ``mpris:artUrl``
— the image isn't downloaded or cached here. Disabled unless
``[Cover] mympd_uri`` points at a reachable myMPD. One entry point,
``cover_url``; async (the urllib call runs in a worker thread).
"""

from __future__ import annotations

import asyncio
import json
import logging

from mpdris2 import _http

logger = logging.getLogger(__name__)

_METHOD = "MYMPD_API_WEBRADIODB_RADIO_GET_BY_URI"


async def cover_url(base_url: str, stream_url: str) -> str | None:
    """Return the WebradioDB cover URL the myMPD at ``base_url`` has for
    ``stream_url``, or ``None`` when it's unknown. Raises on a network/
    transient error so the caller can skip caching and retry."""
    logger.debug("mympd: lookup %s via %s", stream_url, base_url)
    return await asyncio.to_thread(_lookup_blocking, base_url, stream_url)


def _lookup_blocking(base_url: str, stream_url: str) -> str | None:
    # A clean miss returns None; a network/transient error propagates so the
    # caller can skip caching and retry later.
    endpoint = base_url.rstrip("/") + "/api/default"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 0,
        "method": _METHOD,
        "params": {"uri": stream_url},
    }).encode()
    # Miss → JSON-RPC "error" object (no "result"); .get chain yields None.
    image = json.loads(_http.post(endpoint, body)).get("result", {}).get("Image")
    if image:
        logger.debug("mympd: %s -> %s", stream_url, image)
        return str(image)
    logger.debug("mympd: no entry/image for %s", stream_url)
    return None
