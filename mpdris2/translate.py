"""Pure MPD → MPRIS shape conversions.

No D-Bus, no asyncio, no I/O — just shape conversion + tag mapping +
``dbus_fast.Variant`` wrapping. Covers both currentsong() → MPRIS
Metadata (``mpd_to_mpris``) and the smaller per-field status() helpers
(``parse_volume``, ``parse_elapsed``, ``playback_status_from``,
``loop_status_from``) the bridge needs on every refresh.

Keeping these side-effect-free makes them trivial to unit-test and
reusable: cover lookup, for instance, runs separately and adds
``mpris:artUrl`` on top of ``mpd_to_mpris``'s result.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from pathlib import Path

from dbus_fast import Variant

# Tags whose MPD value may legitimately be a list (multiple artists,
# multiple genres, …). For single-valued MPD tags we still wrap as a
# list when the MPRIS key is `as`-typed.
_LIST_TAGS = frozenset({"artist", "albumartist", "composer", "genre"})

# Default URL schemes recognised as "already a URL"; daemon overrides
# this from MPD's ``urlhandlers`` command at startup when available.
DEFAULT_URL_HANDLERS = ("http://", "https://", "mms://", "cdda://", "file://")


def _to_list(val: object) -> list[str]:
    if isinstance(val, list):
        return [str(x) for x in val]
    return [str(val)]


def first(val: object) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val)


def split_title(title: str) -> tuple[str, str] | None:
    """Web-radio ICY titles are usually ``Artist - Track``. Split on the
    first `` - ``; ``None`` when there's no separator (jingles, promos,
    bare station names) so callers never query a backend with junk."""
    artist, sep, track = title.partition(" - ")
    artist, track = artist.strip(), track.strip()
    if not sep or not artist or not track:
        return None
    return artist, track


def _parse_leading_int(s: str) -> int | None:
    m = re.match(r"^(\d+)", s)
    return int(m.group(1)) if m else None


# --- status() helpers -----------------------------------------------------


def playback_status_from(state: str) -> str:
    """MPD ``state`` -> MPRIS ``PlaybackStatus``. Unknown values map to
    ``Stopped`` so a malformed status never makes MPRIS lie."""
    return {"play": "Playing", "pause": "Paused", "stop": "Stopped"}.get(state, "Stopped")


def loop_status_from(repeat: bool, single: bool) -> str:
    """MPD's two-flag (repeat, single) -> MPRIS ``LoopStatus``.
    ``single`` without ``repeat`` doesn't loop, hence ``None``."""
    if repeat and single:
        return "Track"
    if repeat:
        return "Playlist"
    return "None"


def parse_loop_flags(status: dict) -> tuple[bool, bool]:
    """Extract MPD ``(repeat, single)`` flags as booleans. Bridge keeps
    ``repeat`` separately for ``CanGoNext`` (repeat ⇒ playlist wraps)."""
    return (
        status.get("repeat", "0") == "1",
        status.get("single", "0") == "1",
    )


def parse_shuffle(status: dict) -> bool:
    return bool(status.get("random", "0") == "1")


def parse_volume(status: dict) -> float | None:
    """Return MPRIS-style volume (0.0..1.0) from MPD status, or None
    when MPD reports -1 (audio backend can't report — leave as-is)."""
    try:
        v = int(status.get("volume", -1))
    except (TypeError, ValueError):
        return None
    return v / 100.0 if v >= 0 else None


def parse_elapsed(status: dict) -> float:
    try:
        return float(status.get("elapsed", 0.0))
    except (TypeError, ValueError):
        return 0.0


def song_url(
    song: dict,
    music_dir: Path | None = None,
    url_handlers: Iterable[str] = DEFAULT_URL_HANDLERS,
) -> str:
    """Resolve MPD's ``file`` field into a MPRIS-facing URI. Returns ``""``
    when no file is set. Schemes in ``url_handlers`` are passed through
    untouched; relative paths get absolutised against ``music_dir``
    (when set) and turned into ``file://`` URIs."""
    file_uri = first(song.get("file", "")) if song else ""
    if not file_uri:
        return ""
    if any(file_uri.startswith(h) for h in url_handlers) or not music_dir:
        return file_uri
    return (music_dir / file_uri).as_uri()


# --- currentsong() -> Metadata --------------------------------------------


def mpd_to_mpris(
    song: dict,
    music_dir: Path | None = None,
    url_handlers: Iterable[str] = DEFAULT_URL_HANDLERS,
) -> dict[str, Variant]:
    """Translate ``song`` (the dict returned by ``MPD.currentsong()``)
    to an MPRIS Metadata dict with ``Variant``-wrapped values.

    ``music_dir`` is the local filesystem path used to absolutise
    relative MPD paths into a proper ``xesam:url``. ``url_handlers``
    lists URI schemes MPD already returns as-is so we don't prepend
    ``music_dir`` to them.
    """
    out: dict[str, Variant] = {}
    if not song:
        return out

    def setv(key: str, sig: str, val: object) -> None:
        out[key] = Variant(sig, val)

    # --- string tags --------------------------------------------------
    for mpd_key, mpris_key in (("title", "xesam:title"),
                               ("album", "xesam:album")):
        if mpd_key in song:
            setv(mpris_key, "s", first(song[mpd_key]))

    # --- list-valued tags --------------------------------------------
    for mpd_key, mpris_key in (("artist", "xesam:artist"),
                               ("albumartist", "xesam:albumArtist"),
                               ("composer", "xesam:composer"),
                               ("genre", "xesam:genre")):
        if mpd_key in song:
            setv(mpris_key, "as", _to_list(song[mpd_key]))

    # CDDA / CUE tracks frequently carry only ``albumartist``. MPRIS
    # clients overwhelmingly read ``xesam:artist`` for the track-row
    # artist column, so mirror albumArtist into artist when artist is
    # missing.
    if "xesam:artist" not in out and "xesam:albumArtist" in out:
        out["xesam:artist"] = out["xesam:albumArtist"]

    # --- identifiers --------------------------------------------------
    if "id" in song:
        setv("mpris:trackid", "o", f"/org/mpris/MediaPlayer2/Track/{first(song['id'])}")

    # --- duration -----------------------------------------------------
    # MPD has both ``time`` (seconds, deprecated) and ``duration``
    # (seconds, float, MPD >= 0.20). Prefer ``duration`` when present.
    duration_s: float | None = None
    if "duration" in song:
        with contextlib.suppress(TypeError, ValueError):
            duration_s = float(first(song["duration"]))
    elif "time" in song:
        with contextlib.suppress(TypeError, ValueError):
            duration_s = float(first(song["time"]))
    if duration_s is not None and duration_s > 0:
        setv("mpris:length", "x", int(duration_s * 1_000_000))

    # --- dates --------------------------------------------------------
    if "date" in song:
        date = first(song["date"])
        # MPRIS expects ISO-8601-ish; mpDris2 historically just kept the
        # leading year. Anything more elaborate is below the noise floor
        # for MPRIS clients.
        if len(date) >= 4 and date[:4].isdigit():
            setv("xesam:contentCreated", "s", date[:4])

    # --- track / disc numbers ----------------------------------------
    if "track" in song:
        n = _parse_leading_int(first(song["track"]))
        if n is not None:
            # Ensure the integer fits in a signed int32 — MPRIS uses ``i``.
            if n & 0x80000000:
                n -= 0x100000000
            setv("xesam:trackNumber", "i", n)
    if "disc" in song:
        n = _parse_leading_int(first(song["disc"]))
        if n is not None:
            setv("xesam:discNumber", "i", n)

    # --- stream-style metadata fallback -------------------------------
    # Some streams (web radio) only set ``name`` and ``title``: derive
    # an album/title from ``name`` so MPRIS clients have something to
    # display.
    if "name" in song:
        if "xesam:title" not in out:
            setv("xesam:title", "s", first(song["name"]))
        elif "xesam:album" not in out:
            setv("xesam:album", "s", first(song["name"]))

    # --- url ----------------------------------------------------------
    url = song_url(song, music_dir, url_handlers)
    if url:
        setv("xesam:url", "s", url)

    return out
