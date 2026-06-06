"""Shared stdlib helpers for the no-auth cover-art fallbacks
(``deezer`` / ``itunes`` / ``mympd`` / ``radiobrowser``): a thin urllib
wrapper carrying mpDris2's User-Agent + timeout, plus the loose artist
matcher ``deezer``/``itunes`` use to validate a search hit.

``musicbrainz`` goes through the ``musicbrainzngs`` library instead and
keeps its own (accent-folding, fuzzy) matcher.
"""

from __future__ import annotations

import re
import urllib.request

from mpdris2 import __version__

_TIMEOUT = 10
_HEADERS = {"User-Agent": f"mpDris2/{__version__} (https://github.com/b0bbywan/mpDris2)"}
_NORM = re.compile(r"[^a-z0-9]+")


def get(url: str) -> bytes:
    """GET ``url`` and return the raw body; transport errors propagate."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (http(s) only)
        return bytes(resp.read())


def post(url: str, body: bytes) -> bytes:
    """POST the JSON ``body`` to ``url`` and return the raw body."""
    headers = {**_HEADERS, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (http(s) only)
        return bytes(resp.read())


def normalize(s: str) -> str:
    return _NORM.sub(" ", s.lower()).strip()


def artist_matches(query: str, candidate: str) -> bool:
    """Loose containment match (case/punctuation-insensitive) — enough to
    confirm a search hit is the right artist without over-rejecting."""
    q, c = normalize(query), normalize(candidate)
    return bool(q) and bool(c) and (q == c or q in c or c in q)
