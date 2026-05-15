"""Pure translation: MPD song dict -> MPRIS Metadata dict.

No D-Bus, no asyncio, no I/O — just shape conversion + tag mapping +
``dbus_fast.Variant`` wrapping. Keeping this module side-effect-free
makes the metadata mapping easy to unit-test in isolation and
trivially reusable from any caller (cover lookup runs separately and
adds ``mpris:artUrl`` on top of the result).
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


def _first(val: object) -> str:
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val)


def _parse_leading_int(s: str) -> int | None:
    m = re.match(r"^(\d+)", s)
    return int(m.group(1)) if m else None


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
            setv(mpris_key, "s", _first(song[mpd_key]))

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
        setv("mpris:trackid", "o", f"/org/mpris/MediaPlayer2/Track/{_first(song['id'])}")

    # --- duration -----------------------------------------------------
    # MPD has both ``time`` (seconds, deprecated) and ``duration``
    # (seconds, float, MPD >= 0.20). Prefer ``duration`` when present.
    duration_s: float | None = None
    if "duration" in song:
        with contextlib.suppress(TypeError, ValueError):
            duration_s = float(_first(song["duration"]))
    elif "time" in song:
        with contextlib.suppress(TypeError, ValueError):
            duration_s = float(_first(song["time"]))
    if duration_s is not None and duration_s > 0:
        setv("mpris:length", "x", int(duration_s * 1_000_000))

    # --- dates --------------------------------------------------------
    if "date" in song:
        date = _first(song["date"])
        # MPRIS expects ISO-8601-ish; mpDris2 historically just kept the
        # leading year. Anything more elaborate is below the noise floor
        # for MPRIS clients.
        if len(date) >= 4 and date[:4].isdigit():
            setv("xesam:contentCreated", "s", date[:4])

    # --- track / disc numbers ----------------------------------------
    if "track" in song:
        n = _parse_leading_int(_first(song["track"]))
        if n is not None:
            # Ensure the integer fits in a signed int32 — MPRIS uses ``i``.
            if n & 0x80000000:
                n -= 0x100000000
            setv("xesam:trackNumber", "i", n)
    if "disc" in song:
        n = _parse_leading_int(_first(song["disc"]))
        if n is not None:
            setv("xesam:discNumber", "i", n)

    # --- stream-style metadata fallback -------------------------------
    # Some streams (web radio) only set ``name`` and ``title``: derive
    # an album/title from ``name`` so MPRIS clients have something to
    # display.
    if "name" in song:
        if "xesam:title" not in out:
            setv("xesam:title", "s", _first(song["name"]))
        elif "xesam:album" not in out:
            setv("xesam:album", "s", _first(song["name"]))

    # --- url ----------------------------------------------------------
    if "file" in song:
        song_url = _first(song["file"])
        if not any(song_url.startswith(h) for h in url_handlers) and music_dir:
            song_url = (music_dir / song_url).as_uri()
        setv("xesam:url", "s", song_url)

    return out
