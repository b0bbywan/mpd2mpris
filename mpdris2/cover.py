"""Cover-art resolution: async pipeline.

Ordered authoritative-first — the embedded picture is guaranteed to match
the track, a ``cover.jpg`` in the directory might not. Steps 1-4 yield
local bytes (→ tempfile) or a ``file://``; steps 5-7 yield a remote URL
used as ``mpris:artUrl`` unchanged — never downloaded.

1. MPD ``readpicture`` — embedded picture, parsed server-side.
2. Filesystem regex in the song's directory (local FS only).
3. MPD ``albumart`` — ``cover.{png,jpg,jxl,webp}``, resolved server-side.
4. CUE/cdda fallback — for virtual tracks (``cdda://``, ``sheet.cue/trackNNNN``)
   with no on-disk cover, look next to the loaded ``.cue`` (FS, then albumart).
5. Remote cover URL — MusicBrainz/CAA, then opt-in iTunes/Deezer; memoised
   per (artist, album). For web radio (title only) the key is first
   recovered from MusicBrainz, then Deezer.
6. Web-radio station favicon — radio-browser, http(s):// streams.
7. myMPD WebradioDB cover — opt-in (``[Cover] mympd_uri``), http(s):// streams.

Steps 6 and 7 are tried in ``[Cover] stream_sources`` priority order,
first hit wins — list ``mympd`` before ``radiobrowser`` to prefer the
curated WebradioDB cover over the raw station favicon.

Requires MPD ≥ 0.22 for ``readpicture``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import tempfile
import urllib.parse
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, TypeVar

from mpdris2 import deezer, itunes, musicbrainz, mympd, radiobrowser
from mpdris2.translate import first, split_title

logger = logging.getLogger(__name__)

_K = TypeVar("_K")
_V = TypeVar("_V")

DEFAULT_COVER_REGEX = re.compile(
    r"^(album|cover|\.?folder|front).*\.(gif|jpe?g|png|webp|bmp)$",
    re.I | re.X,
)

_MIME_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "image/webp"),  # very rough — WebP starts with RIFF....WEBP
    (b"BM", "image/bmp"),
)
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _detect_mime(data: bytes) -> str | None:
    """Return the MIME type for ``data`` based on its magic bytes, or
    ``None`` when nothing matches — better to skip the cover than to
    serve unknown bytes mislabelled as JPEG."""
    for magic, mime in _MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    return None


_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

# A CUE virtual track is ``path/to/sheet.cue/trackNNNN`` — its audio lives
# elsewhere, so MPD's readpicture/albumart fail with ``Unrecognized URI``.
# Regular tracks never match this shape.
_VIRTUAL_CUE_TRACK_RE = re.compile(r"\.cue/track\d+$", re.I)


def _has_uri_scheme(s: str) -> bool:
    return bool(_URI_SCHEME_RE.match(s))


def _is_virtual_cue_track(song_file: str) -> bool:
    return bool(_VIRTUAL_CUE_TRACK_RE.search(song_file))


async def _fetch_binary(
    cmd: Callable[[str], Awaitable[dict[str, Any]]],
    path: str,
) -> bytes | None:
    """Call an MPD picture-returning command (``readpicture`` /
    ``albumart``), swallow command-level errors, return the binary
    payload or ``None``."""
    try:
        r = await cmd(path)
    except Exception as e:
        logger.debug("%s %r failed: %r", cmd.__name__, path, e)
        return None
    if r and "binary" in r:
        return bytes(r["binary"])
    return None


@dataclass(frozen=True)
class _CoverSource:
    """A cover source, with one optional lookup per kind. Fills in only the
    kinds it supports; the pipeline calls whichever matches the track."""

    name: str
    album: Callable[[str, str], Awaitable[str | None]] | None = None   # (artist, album)
    track: Callable[[str, str], Awaitable[str | None]] | None = None   # (artist, track)
    stream: Callable[[str], Awaitable[str | None]] | None = None       # (stream_url)


@dataclass(frozen=True)
class CoverFinderConfig:
    """Construction-time settings for ``CoverFinder``. Capability flags
    can still be flipped post-init via ``update_capabilities`` once the
    daemon has probed MPD's command list."""

    music_dir: Path | None = None
    cover_regex: re.Pattern[str] = DEFAULT_COVER_REGEX
    can_readpicture: bool = False
    can_albumart: bool = False
    # Step-5 cover-URL sources by name, in priority order (``[Cover] sources``);
    # empty = none. Valid names in _SOURCE_BUILDERS; unknown ones are ignored.
    cover_sources: tuple[str, ...] = ()
    # Web-radio stream cover sources (steps 6-7) by name, in priority order
    # (``[Cover] stream_sources``): ``radiobrowser`` / ``mympd``. empty = none.
    stream_sources: tuple[str, ...] = ()
    mympd_url: str | None = None  # myMPD base URL the ``mympd`` source needs


def _album_track_source(name: str, module: Any) -> _CoverSource:
    return _CoverSource(name, album=module.cover_url, track=module.cover_for_track)


def _mympd_source(config: CoverFinderConfig) -> _CoverSource | None:
    if not config.mympd_url:
        return None  # disabled without a base URL
    base = config.mympd_url  # narrow to str for the closure
    return _CoverSource("mympd", stream=lambda url: mympd.cover_url(base, url))


# Cover sources by ``[Cover] sources`` / ``stream_sources`` name; a builder
# returns ``None`` when a prerequisite is unmet.
_SOURCE_BUILDERS: dict[str, Callable[[CoverFinderConfig], _CoverSource | None]] = {
    "musicbrainz": lambda _c: _album_track_source("musicbrainz", musicbrainz),
    "itunes": lambda _c: _album_track_source("itunes", itunes),
    "deezer": lambda _c: _album_track_source("deezer", deezer),
    "radiobrowser": lambda _c: _CoverSource("radiobrowser", stream=radiobrowser.station_icon),
    "mympd": _mympd_source,
}


@dataclass(frozen=True)
class SongLookup:
    """Everything ``CoverFinder.find`` needs about one song. ``song_uri``
    is the MPRIS-facing URI we cache against; ``song_file`` is the raw
    MPD ``file`` field (may be a relative path, a stream URL, or a CUE
    virtual track)."""

    client: Any
    song_uri: str
    song_file: str
    mpd_meta: dict
    last_loaded_playlist: str = ""


# Per-cache entry cap — covers/keys are one network call to re-resolve.
_CACHE_MAX = 256


class _BoundedCache(OrderedDict):
    """Insertion-ordered dict capped at ``maxsize``, evicting the oldest on
    overflow. Stores ``None`` (a miss), so probe membership with ``in``."""

    def __init__(self, maxsize: int = _CACHE_MAX) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, value)
        if len(self) > self._maxsize:
            self.popitem(last=False)


class CoverFinder:
    """Owns the per-track temp file for embedded covers + the MPD
    capability flags (``readpicture`` / ``albumart``)."""

    def __init__(self, config: CoverFinderConfig | None = None) -> None:
        config = config or CoverFinderConfig()
        self._music_dir = config.music_dir
        self._cover_regex = config.cover_regex
        self._can_readpicture = config.can_readpicture
        self._can_albumart = config.can_albumart
        # ``sources`` feed step 5 (album/track), ``stream_sources`` steps 6-7.
        self._sources = self._resolve_sources(config, config.cover_sources)
        self._stream_sources = self._resolve_sources(config, config.stream_sources)
        self._temp_song_uri: str | None = None
        self._temp_cover: IO[bytes] | None = None
        # Aggregate result caches, one per lookup kind (``None`` = a cached miss).
        self._album_cache: dict[tuple[str, str], str | None] = _BoundedCache()  # step 5, tagged
        self._track_cache: dict[str, str | None] = _BoundedCache()  # step 5, web radio
        self._stream_cache: dict[str, str | None] = _BoundedCache()  # steps 6-7

    @staticmethod
    def _resolve_sources(config: CoverFinderConfig, names: tuple[str, ...]) -> list[_CoverSource]:
        """Named sources in priority order; unknown names and unmet
        prerequisites are skipped with a warning."""
        out: list[_CoverSource] = []
        for name in names:
            builder = _SOURCE_BUILDERS.get(name)
            if builder is None:
                logger.warning("cover: ignoring unknown [Cover] source %r", name)
                continue
            source = builder(config)
            if source is None:
                logger.warning("cover: source %r listed but its prerequisites are unset; skipping", name)
                continue
            out.append(source)
        return out

    def update_capabilities(self, *, can_readpicture: bool, can_albumart: bool) -> None:
        self._can_readpicture = can_readpicture
        self._can_albumart = can_albumart

    def update_music_dir(self, music_dir: Path | None) -> None:
        self._music_dir = music_dir

    # --- local song path resolution ----------------------------------
    def _song_path(self, song_uri: str) -> Path | None:
        if song_uri.startswith("file://"):
            return Path(urllib.parse.unquote(song_uri.removeprefix("file://")))
        if song_uri.startswith("local:track:") and self._music_dir:
            return self._music_dir / urllib.parse.unquote(song_uri.removeprefix("local:track:"))
        return None

    # --- public entry point ------------------------------------------
    async def find(self, req: SongLookup) -> str | None:
        song_path = self._song_path(req.song_uri)
        song_dir = song_path.parent if song_path else None

        # 0. Reuse the existing temp file if we already resolved this track.
        if self._temp_cover is not None:
            if req.song_uri == self._temp_song_uri:
                return Path(self._temp_cover.name).as_uri()
            self._discard_temp()

        # readpicture/albumart need real audio bytes: skip URI schemes
        # (readpicture stalls the MPD connection on these) and CUE virtual
        # tracks. Step 4 picks up both.
        can_query_picture = (
            bool(req.song_file) and not _has_uri_scheme(req.song_file) and not _is_virtual_cue_track(req.song_file)
        )

        # 1. MPD readpicture — embedded picture inside the audio file.
        if can_query_picture:
            data = await self._try_readpicture(req.client, req.song_file)
            cover = self._materialise_bytes(req.song_uri, data, req.song_file)
            if cover:
                return cover

        # 2. Filesystem regex in the song's directory — local FS, direct URI.
        cover = await self._scan_song_dir(song_dir)
        if cover:
            return cover

        # 3. MPD albumart — cover.{jpg,png,…} from the song's directory.
        if can_query_picture:
            data = await self._try_albumart(req.client, req.song_file)
            cover = self._materialise_bytes(req.song_uri, data, req.song_file)
            if cover:
                return cover

        # 4. CUE/cdda fallback — virtual track, look next to the .cue.
        if req.mpd_meta:
            cover = await self._cue_fallback(req)
            if cover:
                return cover

        # 5. Remote cover URL (MusicBrainz/CAA, iTunes, Deezer) — keyed on the
        #    (artist, album) tags, or resolved per-source from a web-radio title.
        artist = first(req.mpd_meta.get("artist"))
        album = first(req.mpd_meta.get("album"))
        title = first(req.mpd_meta.get("title"))
        if artist and album:
            cover = await self._remote_cover(artist, album)
        elif title:
            cover = await self._remote_cover_for_title(title)
        else:
            cover = None
        if cover:
            return cover

        # 6 + 7. Web-radio stream covers (radiobrowser / myMPD), in the
        #        configured priority order.
        cover = await self._stream_cover(req.song_file)
        if cover:
            return cover

        logger.debug("cover: no cover found for %s", req.song_uri)
        return None

    # --- step 1 + 3 helpers: MPD protocol ----------------------------
    async def _try_readpicture(self, client: Any, path: str) -> bytes | None:
        if not self._can_readpicture:
            return None
        return await _fetch_binary(client.readpicture, path)

    async def _try_albumart(self, client: Any, path: str) -> bytes | None:
        if not self._can_albumart:
            return None
        return await _fetch_binary(client.albumart, path)

    async def _cue_fallback(self, req: SongLookup) -> str | None:
        """A CUE virtual track has no on-disk cover; look in the directory
        holding the ``.cue`` instead — FS regex scan first (picks up names
        like ``folder.jpg``), then MPD ``albumart``."""
        cue_dir = self._resolve_cue_dir(req)
        if not cue_dir:
            return None
        if self._music_dir:
            cover = await self._scan_song_dir(self._music_dir / cue_dir)
            if cover:
                return cover
        # albumart scans the parent dir server-side; the suffix is just a hint.
        data = await self._try_albumart(req.client, str(cue_dir / "cover"))
        return self._materialise_bytes(req.song_uri, data, req.song_file)

    def _resolve_cue_dir(self, req: SongLookup) -> Path | None:
        """Directory holding the CUE container, relative to ``music_dir``.
        Prefer ``status.lastloadedplaylist`` when MPD set it (i.e. the
        playlist was added via ``load``); otherwise infer from
        ``song_file`` itself."""
        return self._cue_dir_from_playlist(req.last_loaded_playlist) or self._cue_dir_from_song_file(req.song_file)

    def _cue_dir_from_playlist(self, playlist: str) -> Path | None:
        """``status.lastloadedplaylist`` is an absolute path on disk when
        the CUE was added via ``load``. Strip the ``music_dir`` prefix
        (if any) and return the parent directory."""
        if not playlist:
            return None
        if self._music_dir:
            md_str = str(self._music_dir)
            if playlist.startswith(md_str):
                playlist = playlist[len(md_str) :]
        cue_dir = Path(playlist.lstrip("/")).parent
        return cue_dir if str(cue_dir) not in ("", ".") else None

    def _cue_dir_from_song_file(self, song_file: str) -> Path | None:
        """A CUE virtual track has the form ``dir/sheet.cue/trackNNNN``.
        The grandparent is the cue dir — needed when
        ``lastloadedplaylist`` is empty (CUE added via ``add`` rather
        than ``load``). No filesystem check: ``_is_virtual_cue_track``
        is a reliable shape marker, so this works without ``music_dir``
        being configured."""
        if not song_file or not _is_virtual_cue_track(song_file):
            return None
        grand = Path(song_file).parent.parent
        return grand if str(grand) not in ("", ".") else None

    def _materialise_bytes(
        self,
        song_uri: str,
        data: bytes | None,
        log_origin: str,
    ) -> str | None:
        """Wrap mime detection + materialise; returns None for empty or
        unrecognised data and logs a warning in the latter case."""
        if not data:
            return None
        mime = _detect_mime(data)
        if mime is None:
            logger.warning(
                "MPD returned %d bytes of unrecognised image data for %r; skipping",
                len(data),
                log_origin,
            )
            return None
        return self._materialise(song_uri, data, mime)

    # --- step 2: filesystem regex -----------------------------------
    async def _scan_song_dir(self, song_dir: Path | None) -> str | None:
        if not song_dir:
            return None

        def _scan() -> str | None:
            if not song_dir.is_dir():
                return None
            try:
                # Sort: iterdir() order is filesystem-dependent.
                entries = sorted(song_dir.iterdir(), key=lambda e: e.name)
            except OSError as e:
                logger.debug("cover: scan %s failed: %s", song_dir, e)
                return None
            for entry in entries:
                if self._cover_regex.match(entry.name):
                    logger.debug("cover: regex matched %r in %s", entry.name, song_dir)
                    return entry.as_uri()
            return None

        return await asyncio.to_thread(_scan)

    # --- step 5: remote cover URL -----------------------------------
    async def _remote_cover(self, artist: str, album: str) -> str | None:
        """First cover URL from the album sources for a tagged (artist, album)."""
        return await self._cached_lookup(
            self._album_cache, (artist, album),
            lambda: self._first_cover(self._sources, lambda s: s.album, (artist, album)),
        )

    async def _remote_cover_for_title(self, title: str) -> str | None:
        """First cover URL from the album sources for a web-radio title. The ICY
        title is split once here into (artist, track) and handed to each source,
        which resolves the album within its own catalogue — so a wrong guess from
        one doesn't poison the others."""
        parsed = split_title(title)
        if parsed is None:
            return None
        artist, track = parsed
        return await self._cached_lookup(
            self._track_cache, title,
            lambda: self._first_cover(self._sources, lambda s: s.track, (artist, track)),
        )

    # --- steps 6 + 7: web-radio stream covers -----------------------
    async def _stream_cover(self, stream_url: str) -> str | None:
        """First cover URL from the stream sources for an http(s):// stream,
        tried in priority (list) order."""
        if not self._stream_sources or not stream_url.startswith(("http://", "https://")):
            return None
        return await self._cached_lookup(
            self._stream_cache, stream_url,
            lambda: self._first_cover(self._stream_sources, lambda s: s.stream, (stream_url,)),
        )

    async def _first_cover(
        self,
        sources: list[_CoverSource],
        kind: Callable[[_CoverSource], Callable[..., Awaitable[str | None]] | None],
        args: tuple[str, ...],
    ) -> str | None:
        """First URL from the sources' ``kind`` lookup, tried in priority order
        and stopping at the first hit (cover is off the critical path, so spare
        the lower-priority APIs). An erroring source is skipped but its error
        re-raised if nothing else hit, so the transient failure isn't cached."""
        error: Exception | None = None
        for source in sources:
            fn = kind(source)
            if fn is None:
                continue
            try:
                res = await fn(*args)
            except Exception as e:
                logger.debug("cover: %s failed for %s: %r", source.name, args, e)
                error = error or e
                continue
            if res:
                logger.debug("cover: %s -> %s", source.name, res)
                return res  # first (highest-priority) hit
        if error is not None:
            raise error
        return None

    async def _cached_lookup(
        self,
        cache: dict[_K, _V | None],
        key: _K,
        lookup: Callable[[], Awaitable[_V | None]],
    ) -> _V | None:
        """Serve ``key`` from ``cache``, else run ``lookup()`` and store it.
        A raised error returns ``None`` uncached, so it's retried."""
        if key in cache:
            return cache[key]
        try:
            value = await lookup()
        except Exception as e:
            logger.debug("cover: lookup for %s failed: %r", key, e)
            return None
        cache[key] = value
        return value

    # --- internal helpers --------------------------------------------
    def _materialise(self, song_uri: str, data: bytes, mime: str) -> str:
        ext = _MIME_EXT.get(mime, ".jpg")
        # delete=True is fine: _discard_temp closes it on exit, a hard kill
        # only leaks a few KB on tmpfs. The caller holds it past this function
        # (self._temp_cover), hence the SIM115 silence.
        tmp = tempfile.NamedTemporaryFile(prefix="cover-", suffix=ext)  # noqa: SIM115
        tmp.write(data)
        tmp.flush()
        self._temp_cover = tmp
        self._temp_song_uri = song_uri
        logger.debug("cover: stored embedded image at %r", tmp.name)
        return Path(tmp.name).as_uri()

    def close(self) -> None:
        """Release the per-track temp cover. Daemon calls this at shutdown."""
        self._discard_temp()

    def _discard_temp(self) -> None:
        if self._temp_cover is not None:
            with contextlib.suppress(Exception):
                self._temp_cover.close()
            self._temp_cover = None
            self._temp_song_uri = None
