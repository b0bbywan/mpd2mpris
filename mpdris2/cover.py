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
5. XDG cover cache (``$XDG_CACHE_HOME/mpDris2/{artist}-{album}.jpg``).

The optional MusicBrainz / Cover Art Archive fallback (PR 5) will slot
in as a sixth step.

Requires MPD ≥ 0.22 (for ``readpicture``); the daemon won't error out
on older servers but covers for non-standardly-named files won't work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

logger = logging.getLogger(__name__)

# User-side cover cache. Follows the XDG cache spec; PR 5
# (MusicBrainz fallback) writes its downloads here and step 4 picks
# them up on the next track.
DEFAULT_COVER_CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "mpDris2"
)

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


def _has_uri_scheme(s: str) -> bool:
    return bool(_URI_SCHEME_RE.match(s))


async def _fetch_binary(
    cmd: Callable[[str], Awaitable[dict[str, Any]]], path: str,
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
    cover_cache_dir: Path = DEFAULT_COVER_CACHE_DIR
    can_readpicture: bool = False
    can_albumart: bool = False


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


class CoverFinder:
    """Owns the per-track temp file for embedded covers + the MPD
    capability flags (``readpicture`` / ``albumart``)."""

    def __init__(self, config: CoverFinderConfig | None = None) -> None:
        config = config or CoverFinderConfig()
        self._music_dir = config.music_dir
        self._cover_regex = config.cover_regex
        self._cache_dir = config.cover_cache_dir
        self._can_readpicture = config.can_readpicture
        self._can_albumart = config.can_albumart
        self._temp_song_uri: str | None = None
        self._temp_cover: IO[bytes] | None = None

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
            return self._music_dir / urllib.parse.unquote(
                song_uri.removeprefix("local:track:")
            )
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

        # 1. MPD readpicture — embedded picture inside the audio file.
        #    Skip URI schemes (cdda://, http://, …) since readpicture
        #    against them stalls the MPD connection (commit 234d6da).
        if req.song_file and not _has_uri_scheme(req.song_file):
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
        #    missed. Skip on URI schemes (handled by step 4).
        if req.song_file and not _has_uri_scheme(req.song_file):
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

        # 5. Downloaded-covers cache (XDG).
        cover = self._lookup_downloads_cache(req.mpd_meta)
        if cover:
            return cover

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
        if not req.last_loaded_playlist:
            return None
        playlist = req.last_loaded_playlist
        if self._music_dir:
            md_str = str(self._music_dir)
            if playlist.startswith(md_str):
                playlist = playlist[len(md_str):]
        cue_dir = Path(playlist.lstrip("/")).parent
        if str(cue_dir) in ("", "."):
            return None
        if self._music_dir:
            cover = await self._scan_song_dir(self._music_dir / cue_dir)
            if cover:
                return cover
        # MPD's albumart command scans the file's parent directory
        # server-side for cover.{png,jpg,jxl,webp} — one call is
        # enough, the path-suffix we pass is just a directory hint.
        data = await self._try_albumart(req.client, str(cue_dir / "cover.jpg"))
        return self._materialise_bytes(req.song_uri, data, req.song_file)

    def _materialise_bytes(
        self, song_uri: str, data: bytes | None, log_origin: str,
    ) -> str | None:
        """Wrap mime detection + materialise; returns None for empty or
        unrecognised data and logs a warning in the latter case."""
        if not data:
            return None
        mime = _detect_mime(data)
        if mime is None:
            logger.warning(
                "MPD returned %d bytes of unrecognised image data for %r; skipping",
                len(data), log_origin,
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
            for entry in song_dir.iterdir():
                if self._cover_regex.match(entry.name):
                    logger.debug("cover: regex matched %r in %s", entry.name, song_dir)
                    return entry.as_uri()
            return None

        return await asyncio.to_thread(_scan)

    # --- step 5: downloaded-covers cache ----------------------------
    def _lookup_downloads_cache(self, mpd_meta: dict) -> str | None:
        artist = mpd_meta.get("artist")
        album = mpd_meta.get("album")
        if not artist or not album:
            return None
        artist_key = artist[0] if isinstance(artist, list) else artist
        path = self._cache_dir / f"{artist_key}-{album}.jpg"
        return path.as_uri() if path.exists() else None

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

    def _discard_temp(self) -> None:
        if self._temp_cover is not None:
            with contextlib.suppress(Exception):
                self._temp_cover.close()
            self._temp_cover = None
            self._temp_song_uri = None
