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

from mpdris2 import deezer, itunes, musicbrainz
from mpdris2.translate import first

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
class CoverFinderConfig:
    """Construction-time settings for ``CoverFinder``. Capability flags
    can still be flipped post-init via ``update_capabilities`` once the
    daemon has probed MPD's command list."""

    music_dir: Path | None = None
    cover_regex: re.Pattern[str] = DEFAULT_COVER_REGEX
    can_readpicture: bool = False
    can_albumart: bool = False
    # Opt-in step-5 fallbacks, tried after the always-on MusicBrainz/CAA.
    use_itunes: bool = False
    use_deezer: bool = False
    # Base URL of a myMPD instance for the WebradioDB cover fallback (step
    # 7); empty/None disables it.
    mympd_url: str | None = None


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
        self._use_deezer = config.use_deezer
        # Step-5 cover sources, MusicBrainz/CAA first then the opt-in
        # fallbacks. Each: ``cover_url(artist, album)``.
        self._cover_sources: list[Any] = [musicbrainz]
        if config.use_itunes:
            self._cover_sources.append(itunes)
        if config.use_deezer:
            self._cover_sources.append(deezer)
        self._temp_song_uri: str | None = None
        self._temp_cover: IO[bytes] | None = None
        self._title_key_cache: dict[str, tuple[str, str] | None] = _BoundedCache()
        self._url_cache: dict[tuple[str, str], str | None] = _BoundedCache()  # step 5

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

        # (artist, album) key: from tags, or recovered from MusicBrainz/Deezer
        # for a title-only web-radio stream.
        artist, album = await self._resolve_key(req.mpd_meta)

        # 5. Remote cover URL (MusicBrainz/CAA, iTunes, Deezer).
        cover = await self._remote_cover(artist, album)
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

    # --- (artist, album) key resolution -----------------------------
    async def _resolve_key(self, mpd_meta: dict) -> tuple[str, str]:
        """The (artist, album) the cover lookups key on: from the tags, or
        — for a title-only web-radio stream — recovered from MusicBrainz then
        Deezer. Cached per title so a repeat play resolves the same way."""
        artist = first(mpd_meta.get("artist"))
        album = first(mpd_meta.get("album"))
        if artist and album:
            return artist, album
        title = first(mpd_meta.get("title"))
        if not title:
            return artist, album
        key = await self._cached_definitive(
            self._title_key_cache, title,
            lambda: self._resolve_title(title),
        )
        return key or (artist, album)

    async def _resolve_title(self, title: str) -> tuple[tuple[str, str] | None, bool]:
        """Resolve a web-radio title to (artist, album) from MusicBrainz, then
        Deezer if enabled. Returns ``(key, definitive)``; ``definitive`` is
        False when a source errored and none resolved (caller won't cache)."""
        sources: list[tuple[str, Callable[[str], Awaitable[tuple[str, str] | None]]]] = [
            ("musicbrainz", musicbrainz.resolve_album),
        ]
        if self._use_deezer:
            sources.append(("deezer", deezer.resolve_album))
        results = await asyncio.gather(*(fn(title) for _, fn in sources), return_exceptions=True)
        key: tuple[str, str] | None = None
        errored = False
        for (name, _fn), res in zip(sources, results, strict=True):
            if isinstance(res, BaseException):
                logger.debug("cover: %s resolve failed for %r: %r", name, title, res)
                errored = True
            elif res and key is None:
                key = res  # first (highest-priority) hit
        return key, key is not None or not errored

    # --- step 5: remote cover URL -----------------------------------
    async def _remote_cover(self, artist: str, album: str) -> str | None:
        """First cover URL from MusicBrainz/iTunes/Deezer for an album.
        ``None`` when artist/album is missing or no source has it."""
        if not artist or not album:
            logger.debug("cover: no remote key (artist=%r album=%r)", artist, album)
            return None
        return await self._cached_definitive(
            self._url_cache, (artist, album),
            lambda: self._lookup_cover_url(artist, album),
        )

    async def _lookup_cover_url(self, artist: str, album: str) -> tuple[str | None, bool]:
        """Query the cover sources concurrently; return the MusicBrainz-first
        hit as ``(url, definitive)``. ``definitive`` is False when a source
        errored and none yielded a URL (caller won't cache)."""
        results = await asyncio.gather(
            *(source.cover_url(artist, album) for source in self._cover_sources),
            return_exceptions=True,
        )
        url: str | None = None
        errored = False
        for source, res in zip(self._cover_sources, results, strict=True):
            if isinstance(res, BaseException):
                logger.debug("cover: %s failed for %r / %r: %r", source.__name__, artist, album, res)
                errored = True
            elif res and url is None:
                logger.debug("cover: %s -> %s", source.__name__, res)
                url = res  # first (highest-priority) hit
        return url, url is not None or not errored

    async def _cached_definitive(
        self,
        cache: dict[_K, _V | None],
        key: _K,
        compute: Callable[[], Awaitable[tuple[_V | None, bool]]],
    ) -> _V | None:
        """Like ``_cached_lookup`` but ``compute()`` returns
        ``(value, definitive)``; only a ``definitive`` result is cached, so a
        partial failure (a source errored) is retried."""
        if key in cache:
            return cache[key]
        value, definitive = await compute()
        if definitive:
            cache[key] = value
        return value

    # --- internal helpers --------------------------------------------
    def _materialise(self, song_uri: str, data: bytes, mime: str) -> str:
        ext = _MIME_EXT.get(mime, ".jpg")
        # delete=True cleans up on normal interpreter shutdown via GC,
        # and the daemon calls ``_discard_temp`` explicitly on exit.
        # Hard kills (SIGKILL, OOM) leak the file until /tmp is purged
        # — acceptable since covers are a few KB on tmpfs. Lifetime
        # extends past this function (caller holds via
        # ``self._temp_cover``), hence the SIM115 silence.
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
