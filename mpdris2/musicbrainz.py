"""MusicBrainz / Cover Art Archive lookups (optional dependency).

Isolated so the rest of the daemon never imports ``musicbrainzngs``
directly: when it isn't installed every call here degrades to a no-op.
Two entry points, both async (the library is synchronous and
auto-rate-limited, so the blocking work runs in a worker thread):

* ``resolve_album`` — recover (artist, album) from a free-form title,
  for web-radio streams that only expose an ICY title.
* ``fetch_cover``   — download an album's front cover bytes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

from mpdris2 import __version__
from mpdris2.translate import first, split_title

try:
    import musicbrainzngs
    from rapidfuzz import fuzz
    # musicbrainzngs logs every HTTP request and XML-parser quirk at INFO;
    # quiet it so our own debug output stays readable.
    logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
except ImportError:  # optional feature — needs both deps together
    musicbrainzngs = None
    fuzz = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Cover Art Archive thumbnail size (px).
_IMAGE_SIZE = "500"
# How many recording hits to scan before giving up.
_SEARCH_LIMIT = 5
# rapidfuzz score thresholds (0-100): titles tighter than artists, since a
# subset artist ("Bob Marley" ⊂ "Bob Marley & The Wailers") is legitimate.
_ARTIST_MIN = 85
_TITLE_MIN = 90
_NORM = re.compile(r"[^a-z0-9]+")
# Decorations to drop before comparing titles: "[ft. X]", "(feat Y)",
# "feat. Z" and everything after — radio titles carry these, MB doesn't.
_DECOR = re.compile(r"\[[^\]]*\]|\([^)]*\)|\b(?:feat|ft|featuring)\b.*", re.I)
_useragent_set = False


def _norm(s: str) -> str:
    # Fold "&"/"and" and strip accents so an ICY title matches MB's
    # spelling ("Bob Marley And The Wailers" == "Bob Marley & The Wailers",
    # "Telephone" == "Téléphone").
    s = unicodedata.normalize("NFKD", s.replace("&", " and "))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _NORM.sub(" ", s.lower()).strip()


def _artist_matches(query: str, candidate: str) -> bool:
    """Tolerant of word order and extra members (token-set), so
    ``Bob Marley`` matches ``Bob Marley & The Wailers``."""
    q, c = _norm(query), _norm(candidate)
    return bool(q) and bool(c) and fuzz.token_set_ratio(q, c) >= _ARTIST_MIN


def _title_matches(query: str, candidate: str) -> bool:
    """Stricter (token-sort), after dropping featuring/bracket decorations,
    so a short candidate (``Sunshine``) doesn't match a longer query
    (``Ain't No Sunshine [ft. Sting]``)."""
    q, c = _norm(_DECOR.sub(" ", query)), _norm(_DECOR.sub(" ", candidate))
    return bool(q) and bool(c) and fuzz.token_sort_ratio(q, c) >= _TITLE_MIN


# Secondary release-group types that aren't the canonical studio album —
# their covers are usually absent or off (live bootlegs, comps, …).
_SKIP_SECONDARY = frozenset({
    "Compilation", "Live", "Demo", "Interview", "Soundtrack",
    "Spokenword", "Audiobook", "Mixtape/Street", "DJ-mix", "Remix",
})


def _best_group(groups: list[dict]) -> dict | None:
    """Prefer a plain studio Album release-group (its cover is the
    canonical one and far more likely to exist on the Cover Art Archive);
    fall back to the first hit."""
    for g in groups:
        if g.get("primary-type") == "Album" and not (_SKIP_SECONDARY & set(g.get("secondary-type-list") or [])):
            return g
    return groups[0] if groups else None


def _ensure_useragent() -> None:
    global _useragent_set
    if not _useragent_set:
        musicbrainzngs.set_useragent(
            "mpDris2", __version__, "https://github.com/b0bbywan/mpDris2",
        )
        _useragent_set = True


async def resolve_album(title: str) -> tuple[str, str] | None:
    """Recover (artist, album) from a ``Artist - Track`` title via a
    fielded recording search, validating that the hit actually matches.
    ``None`` without the dependency, an unparseable title, or no
    confident match."""
    if musicbrainzngs is None or not title:
        return None
    parsed = split_title(title)
    if parsed is None:
        return None
    return await asyncio.to_thread(_resolve_blocking, *parsed)


async def fetch_cover(artist: str, album: str) -> bytes | None:
    """Download an album's front cover. ``None`` without the dependency
    or when nothing matches."""
    if musicbrainzngs is None:
        logger.debug("musicbrainz: skipped (not installed)")
        return None
    logger.debug("musicbrainz: cover for %r / %r", artist, album)
    return await asyncio.to_thread(_fetch_blocking, artist, album)


def _resolve_blocking(q_artist: str, q_track: str) -> tuple[str, str] | None:
    # Fielded search (musicbrainzngs escapes the values); keep only a hit
    # whose artist AND recording title actually match the query, so a
    # jingle or a loose coincidence never yields a cover.
    try:
        _ensure_useragent()
        result = musicbrainzngs.search_recordings(
            artist=q_artist, recording=q_track, limit=_SEARCH_LIMIT,
        )
        recordings = result.get("recording-list") or []
        for rec in recordings:
            rec_artist = rec.get("artist-credit-phrase") or ""
            if not (_artist_matches(q_artist, rec_artist) and _title_matches(q_track, rec.get("title") or "")):
                continue
            groups = [r["release-group"] for r in (rec.get("release-list") or []) if r.get("release-group")]
            rg = _best_group(groups)
            album = first(rg.get("title")) if rg else ""
            if album:
                logger.debug("musicbrainz: %r / %r -> %r / %r", q_artist, q_track, rec_artist, album)
                return rec_artist, album
        if recordings:
            top = recordings[0]
            logger.debug("musicbrainz: no confident match for %r / %r (closest: %r / %r, score %s)",
                         q_artist, q_track, top.get("artist-credit-phrase"), top.get("title"), top.get("ext:score"))
        else:
            logger.debug("musicbrainz: no results for %r / %r", q_artist, q_track)
        return None
    except Exception as e:
        logger.debug("musicbrainz: search for %r / %r failed: %r", q_artist, q_track, e)
        return None


def _fetch_blocking(artist: str, album: str) -> bytes | None:
    try:
        _ensure_useragent()
        result = musicbrainzngs.search_release_groups(artist=artist, releasegroup=album, limit=_SEARCH_LIMIT)
        rg = _best_group(result.get("release-group-list") or [])
        if rg is None:
            logger.debug("musicbrainz: no release group for %r / %r", artist, album)
            return None
        return bytes(musicbrainzngs.get_release_group_image_front(rg["id"], size=_IMAGE_SIZE))
    except Exception as e:
        # ResponseError carries the HTTP status in ``.cause`` — surface 404
        # ("no cover art", common and harmless) distinctly from anything
        # else (e.g. 503 rate-limiting) so the logs aren't ambiguous.
        code = getattr(getattr(e, "cause", None), "code", None)
        if code == 404:
            logger.debug("musicbrainz: no cover art for %r / %r", artist, album)
        else:
            logger.debug("musicbrainz: cover lookup for %r / %r failed: %r", artist, album, e)
        return None
