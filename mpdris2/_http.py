"""Shared stdlib HTTP for the no-auth cover-art fallbacks
(``deezer`` / ``itunes`` / ``mympd`` / ``radiobrowser``): a thin urllib
wrapper carrying mpDris2's User-Agent + timeout.

The loose artist matcher those modules use lives in ``translate``
(``artist_matches``). ``musicbrainz`` goes through ``musicbrainzngs`` and
keeps its own (accent-folding, fuzzy) matcher.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from mpdris2 import APP, URL, __version__

_TIMEOUT = 10
_HEADERS = {"User-Agent": f"{APP}/{__version__} ({URL})"}


def get(url: str) -> bytes:
    """GET ``url`` and return the raw body; transport errors propagate."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (http(s) only)
        return bytes(resp.read())


def is_image(url: str) -> bool:
    """HEAD ``url``; True if it serves an image. False on 404/410 or a
    non-image ``Content-Type``. Other errors propagate (caller retries).
    A missing ``Content-Type`` or a HEAD-refusing server gets a pass."""
    req = urllib.request.Request(url, headers=_HEADERS, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (http(s) only)
            ctype = resp.headers.get("Content-Type", "").lower()
            return not ctype or ctype.startswith("image/")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return False
        if e.code in (405, 501):  # HEAD unsupported
            return True
        raise


def post(url: str, body: bytes) -> bytes:
    """POST the JSON ``body`` to ``url`` and return the raw body."""
    headers = {**_HEADERS, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (http(s) only)
        return bytes(resp.read())
