"""myMPD WebradioDB cover fallback (no API key, stdlib only).

Opt-in (``[Cover] mympd_uri``): asks a myMPD instance's WebradioDB for a
curated station ``Image`` URL (used as ``mpris:artUrl``, never downloaded).
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
    endpoint = base_url.rstrip("/") + "/api/default"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 0,
        "method": _METHOD,
        "params": {"uri": stream_url},
    }).encode()
    # Miss → JSON-RPC "error" object (no "result", or an explicit
    # "result": null); ``or {}`` collapses both to a clean None.
    image = (json.loads(_http.post(endpoint, body)).get("result") or {}).get("Image")
    if image:
        logger.debug("mympd: %s -> %s", stream_url, image)
        return str(image)
    logger.debug("mympd: no entry/image for %s", stream_url)
    return None
