"""Cover-art resolution: async pipeline.

Ordered authoritative-first — the picture inside the audio file is
guaranteed to match the track, whereas a ``cover.jpg`` in the song's
directory could be stale or wrong. We accept the extra cost of
parsing/transferring for that guarantee.

1. MPD ``readpicture`` — embedded picture in the audio file. MPD does
   the format-specific parsing server-side, works for both local and
   remote MPD. Bytes → tempfile.
2. Filesystem regex match in the song's directory — only when we have
   local FS access. Returns the file's URI directly, no copying. The
   cheapest step, but ``cover.jpg`` may not match the track exactly.
3. MPD ``albumart`` — MPD resolves ``cover.{png,jpg,jxl,webp}`` from
   the song's directory server-side and ships the bytes. Useful for
   remote MPD or when step 2's regex missed a standard-named cover.
   Bytes → tempfile.
4. CUE/cdda fallback — when ``song_file`` is a virtual reference
   (``cdda://Disc/Track01`` or a CUE playlist track) the audio file
   itself has no on-disk cover; look next to the loaded ``.cue``
   playlist instead. Tries the local FS regex first (no temp-file
   copy, picks up names like ``folder.jpg``), then falls back to MPD
   ``albumart``.
5. Remote cover URL — MusicBrainz/CAA (canonical), then the opt-in
   iTunes and Deezer fallbacks (broader coverage, off by default), each
   returning an image **URL** served verbatim as ``mpris:artUrl`` (no
   download — lighter, and we link rather than re-host). Memoised per
   (artist, album). For web radio
   (only a title, no album tag) the artist+album are first recovered
   from MusicBrainz, then Deezer as a fallback, so this path can key on
   them.
6. Web-radio station favicon URL — last resort for http(s):// streams.

Requires MPD ≥ 0.22 (for ``readpicture``); the daemon won't error out
on older servers but covers for non-standardly-named files won't work.
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
from typing import IO, Any

from mpdris2 import deezer, itunes, musicbrainz, radiobrowser
from mpdris2.translate import first

logger = logging.getLogger(__name__)

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

# A CUE virtual track surfaces as ``path/to/sheet.cue/trackNNNN`` — the
# audio it represents lives elsewhere (stream URL, raw CD, separate
# audio file referenced by the sheet), so MPD's readpicture/albumart on
# this URI always fails with ``Unrecognized URI``. The shape itself is a
# reliable marker: regular tracks never look like this.
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
    # Opt-in remote cover fallbacks (step 5), off by default. MusicBrainz/CAA
    # is always tried first; these widen coverage at the cost of extra
    # third-party queries.
    use_itunes: bool = False
    use_deezer: bool = False


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


# Per-cache entry cap. Covers/keys are cheap to re-resolve (one network
# call), so a long-running daemon zapping web radios needn't hoard them.
_CACHE_MAX = 256


class _BoundedCache(OrderedDict):
    """Insertion-ordered dict capped at ``maxsize`` entries, evicting the
    oldest on overflow. Holds ``None`` values (negative cache), so callers
    probe membership with ``in``."""

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
        # Remote cover-URL sources tried in order, first hit wins:
        # MusicBrainz/CAA (canonical, always on), then the opt-in,
        # broader-coverage, no-auth fallbacks. Each exposes
        # ``cover_url(artist, album) -> str | None``.
        self._cover_sources: list[Any] = [musicbrainz]
        if config.use_itunes:
            self._cover_sources.append(itunes)
        if config.use_deezer:
            self._cover_sources.append(deezer)
        self._temp_song_uri: str | None = None
        self._temp_cover: IO[bytes] | None = None
        # title -> (artist, album) | None, memoised so a web-radio title
        # resolves to a stable key across refreshes (and isn't re-queried).
        self._title_key_cache: dict[str, tuple[str, str] | None] = _BoundedCache()
        # (artist, album) -> remote cover URL | None (step 5), memoised so an
        # album played track-by-track isn't re-looked-up each time.
        self._url_cache: dict[tuple[str, str], str | None] = _BoundedCache()
        # stream URL -> station favicon URL | None (step 6), memoised so the
        # station isn't re-queried on every track.
        self._station_cache: dict[str, str | None] = _BoundedCache()

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

        # readpicture/albumart need a song_file that MPD can resolve to
        # actual audio bytes. Skip URI schemes (cdda://, http://, … —
        # readpicture stalls the MPD connection on these, commit
        # 234d6da) and CUE virtual tracks (``sheet.cue/trackNNNN`` —
        # MPD rejects them with ``Unrecognized URI``). Step 4 picks up
        # both cases.
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

        # 3. MPD albumart — MPD reads cover.{jpg,png,…} from the song's
        #    directory. Useful for remote MPD or when step 2's regex
        #    missed.
        if can_query_picture:
            data = await self._try_albumart(req.client, req.song_file)
            cover = self._materialise_bytes(req.song_uri, data, req.song_file)
            if cover:
                return cover

        # 4. CUE/cdda fallback — the song_file is a virtual reference
        #    (``cdda://Disc/Track01``, ``playlist.cue/track0001``) with
        #    no real on-disk file. Look for a cover next to the loaded
        #    .cue playlist (FS scan first, then MPD ``albumart``).
        if req.mpd_meta:
            cover = await self._cue_fallback(req)
            if cover:
                return cover

        # Resolve the (artist, album) key: straight from the tags, or
        # — for web radio with only a title — recovered from MusicBrainz.
        artist, album = await self._resolve_key(req.mpd_meta)

        # 5. Remote cover URL (MusicBrainz/CAA, iTunes, Deezer), served
        #    verbatim — MPRIS clients fetch the artUrl themselves.
        cover = await self._remote_cover(artist, album)
        if cover:
            return cover

        # 6. Web-radio station favicon URL — last resort for http(s)://
        #    streams (returned as-is; MPRIS clients fetch artUrl themselves).
        cover = await self._station_favicon(req.song_file)
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
        """When ``song_file`` is a CUE virtual track (cdda://, http://,
        …) the audio file itself has no on-disk cover and MPD's
        ``albumart`` on the song path fails. The only useful fallback
        is the directory holding the CUE itself — that's where the
        cover typically lives. Try a local FS regex scan first (no
        temp-file copy, picks up non-standard names like
        ``folder.jpg``); on failure ask MPD's ``albumart`` to resolve
        ``cover.{png,jpg,jxl,webp}`` server-side."""
        cue_dir = self._resolve_cue_dir(req)
        if not cue_dir:
            return None
        if self._music_dir:
            cover = await self._scan_song_dir(self._music_dir / cue_dir)
            if cover:
                return cover
        # MPD's albumart command scans the file's parent directory
        # server-side for cover.{png,jpg,jxl,webp} — one call is
        # enough, the path-suffix we pass is just a directory hint.
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
        """Return the (artist, album) the cache and cover lookups key on.
        From the tags when present; otherwise — web radio with only a
        title — recovered from MusicBrainz, then Deezer (broader catalogue,
        when enabled) as a fallback, memoised per title. The memo is what
        makes a repeat play resolve to the same key (so the disk cache hits)
        instead of re-querying non-deterministically."""
        artist = first(mpd_meta.get("artist"))
        album = first(mpd_meta.get("album"))
        if artist and album:
            return artist, album
        title = first(mpd_meta.get("title"))
        if not title:
            return artist, album
        if title not in self._title_key_cache:
            key = await musicbrainz.resolve_album(title)
            if key is None and self._use_deezer:
                key = await deezer.resolve_album(title)
            self._title_key_cache[title] = key
        return self._title_key_cache[title] or (artist, album)

    # --- step 5: remote cover URL -----------------------------------
    async def _remote_cover(self, artist: str, album: str) -> str | None:
        """First cover URL from MusicBrainz/iTunes/Deezer for an album,
        served verbatim. Memoised per (artist, album); ``None`` when
        artist/album is missing or no source has it."""
        if not artist or not album:
            logger.debug("cover: no remote key (artist=%r album=%r)", artist, album)
            return None
        key = (artist, album)
        if key not in self._url_cache:
            self._url_cache[key] = await self._lookup_cover_url(artist, album)
        return self._url_cache[key]

    async def _lookup_cover_url(self, artist: str, album: str) -> str | None:
        for source in self._cover_sources:
            url: str | None = await source.cover_url(artist, album)
            if url:
                logger.debug("cover: %s -> %s", source.__name__, url)
                return url
        return None

    # --- step 6: web-radio station favicon URL ----------------------
    async def _station_favicon(self, stream_url: str) -> str | None:
        """Favicon URL of the station serving an http(s):// stream, memoised
        per stream URL. Returned verbatim — no download, MPRIS clients fetch
        the remote artUrl themselves."""
        if not stream_url.startswith(("http://", "https://")):
            return None
        if stream_url not in self._station_cache:
            self._station_cache[stream_url] = await radiobrowser.station_icon(stream_url)
        return self._station_cache[stream_url]

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
