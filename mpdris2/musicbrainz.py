"""MusicBrainz / Cover Art Archive lookups (optional dependency).

Isolated so nothing else imports ``musicbrainzngs`` directly: without it,
every call here is a no-op. ``cover_url`` returns an album's front-cover URL
from the CAA (never downloaded); ``cover_for_track`` does the same for a
web-radio (artist, track) by first resolving its album. No image download.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from difflib import SequenceMatcher

from mpdris2 import APP, URL, __version__
from mpdris2.translate import first, normalize

try:
    import musicbrainzngs
    # musicbrainzngs logs every HTTP request and XML-parser quirk at INFO;
    # quiet it so our own debug output stays readable.
    logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
except ImportError:  # optional feature
    musicbrainzngs = None

logger = logging.getLogger(__name__)

# Cover Art Archive thumbnail size (px).
_IMAGE_SIZE = "500"
# How many recording hits to scan before giving up.
_SEARCH_LIMIT = 5
# Similarity thresholds (0-100): titles tighter than artists, since a subset
# artist ("Bob Marley" ⊂ "Bob Marley & The Wailers") is legitimate.
_ARTIST_MIN = 85
_TITLE_MIN = 90


def _ratio(a: str, b: str) -> float:
    """0-100 character-similarity via the stdlib SequenceMatcher."""
    return SequenceMatcher(None, a, b).ratio() * 100


def _token_sort_ratio(a: str, b: str) -> float:
    """Compare after sorting whitespace tokens — order-insensitive, so a short
    candidate still has to cover the whole query (substrings score low)."""
    return _ratio(" ".join(sorted(a.split())), " ".join(sorted(b.split())))


def _token_set_ratio(a: str, b: str) -> float:
    """token-set ratio: weigh the shared tokens against each side's full set,
    so a subset ("Bob Marley" ⊂ "Bob Marley & The Wailers") scores 100. Both
    order- and extra-member-insensitive."""
    t1, t2 = set(a.split()), set(b.split())
    if not t1 or not t2:
        return 0.0
    sect = " ".join(sorted(t1 & t2))
    combined1 = f"{sect} {' '.join(sorted(t1 - t2))}".strip()
    combined2 = f"{sect} {' '.join(sorted(t2 - t1))}".strip()
    return max(_ratio(sect, combined1), _ratio(sect, combined2), _ratio(combined1, combined2))
# Decorations to drop before comparing titles: "[ft. X]", "(feat Y)",
# "feat. Z" and everything after — radio titles carry these, MB doesn't.
_DECOR = re.compile(r"\[[^\]]*\]|\([^)]*\)|\b(?:feat|ft|featuring)\b.*", re.I)
_useragent_set = False


def _norm(s: str) -> str:
    # Fold "&"/"and" and strip accents ("Téléphone" == "Telephone") before
    # translate.normalize's lowercase/alnum collapse.
    s = unicodedata.normalize("NFKD", s.replace("&", " and "))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return normalize(s)


def _artist_matches(query: str, candidate: str) -> bool:
    """Tolerant of word order and extra members (token-set), so
    ``Bob Marley`` matches ``Bob Marley & The Wailers``."""
    q, c = _norm(query), _norm(candidate)
    return bool(q) and bool(c) and _token_set_ratio(q, c) >= _ARTIST_MIN


def _title_matches(query: str, candidate: str) -> bool:
    """Stricter (token-sort), after dropping featuring/bracket decorations,
    so a short candidate (``Sunshine``) doesn't match a longer query
    (``Ain't No Sunshine [ft. Sting]``)."""
    q, c = _norm(_DECOR.sub(" ", query)), _norm(_DECOR.sub(" ", candidate))
    return bool(q) and bool(c) and _token_sort_ratio(q, c) >= _TITLE_MIN


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
        musicbrainzngs.set_useragent(APP, __version__, URL)
        _useragent_set = True


async def resolve_album(artist: str, track: str) -> tuple[str, str] | None:
    """Recover (artist, album) for a web-radio (artist, track) via a fielded
    recording search, validating that the hit actually matches. ``None``
    without the dependency or no confident match."""
    if musicbrainzngs is None or not artist or not track:
        return None
    return await asyncio.to_thread(_resolve_blocking, artist, track)


async def cover_url(artist: str, album: str) -> str | None:
    """Front-cover URL from the Cover Art Archive. ``None`` without the
    dependency or when nothing matches — the CAA image list both confirms
    the cover exists and yields its URL, no image download."""
    if musicbrainzngs is None:
        logger.debug("musicbrainz: skipped (not installed)")
        return None
    logger.debug("musicbrainz: cover for %r / %r", artist, album)
    return await asyncio.to_thread(_url_blocking, artist, album)


async def cover_for_track(artist: str, track: str) -> str | None:
    """Cover URL for a web-radio (artist, track): resolve the album, then its
    CAA cover."""
    key = await resolve_album(artist, track)
    return await cover_url(*key) if key else None


def _resolve_blocking(q_artist: str, q_track: str) -> tuple[str, str] | None:
    # Keep only a hit whose artist AND title match, so a jingle or loose
    # coincidence never yields a cover.
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


def _url_blocking(artist: str, album: str) -> str | None:
    _ensure_useragent()
    result = musicbrainzngs.search_release_groups(artist=artist, releasegroup=album, limit=_SEARCH_LIMIT)
    rg = _best_group(result.get("release-group-list") or [])
    if rg is None:
        logger.debug("musicbrainz: no release group for %r / %r", artist, album)
        return None
    try:
        images = musicbrainzngs.get_release_group_image_list(rg["id"]).get("images") or []
    except Exception as e:
        # 404 = confirmed "no cover art" (a real miss); anything else
        # (rate-limit, network) is transient, so propagate it.
        if getattr(getattr(e, "cause", None), "code", None) == 404:
            logger.debug("musicbrainz: no cover art for %r / %r", artist, album)
            return None
        raise
    for img in images:
        if img.get("front"):
            url = (img.get("thumbnails") or {}).get(_IMAGE_SIZE) or img.get("image")
            if url:
                return str(url)
    logger.debug("musicbrainz: no front cover for %r / %r", artist, album)
    return None
