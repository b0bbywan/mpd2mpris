"""Unit tests for cover.py — pure helpers, filesystem steps, and the
async ``find`` orchestration with a stubbed MPD client.

Mutagen extraction (step 2) is not exercised here: it would need a
real media file with embedded art per format. Covered by integration.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mpd2mpris.cover import (
    CoverFinder,
    CoverFinderConfig,
    SongLookup,
    _BoundedCache,
    _detect_mime,
    _has_uri_scheme,
    _is_virtual_cue_track,
)

# --- _BoundedCache --------------------------------------------------------

def test_bounded_cache_evicts_oldest_over_capacity() -> None:
    c = _BoundedCache(maxsize=2)
    c["a"], c["b"] = 1, 2
    c["c"] = 3  # evicts "a", the oldest
    assert "a" not in c
    assert dict(c) == {"b": 2, "c": 3}


def test_bounded_cache_keeps_none_values() -> None:
    c = _BoundedCache(maxsize=2)
    c["a"] = None
    assert "a" in c and c["a"] is None

# --- _detect_mime ---------------------------------------------------------

@pytest.mark.parametrize("data,expected", [
    (b"\x89PNG\r\n\x1a\n...", "image/png"),
    (b"\xff\xd8\xff\xe0...", "image/jpeg"),
    (b"GIF89a...", "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
    (b"BM\x36\x00\x00\x00...", "image/bmp"),
])
def test_detect_mime_known_magics(data: bytes, expected: str) -> None:
    assert _detect_mime(data) == expected


@pytest.mark.parametrize("data", [b"", b"random garbage", b"TIFF\x00"])
def test_detect_mime_unknown_returns_none(data: bytes) -> None:
    assert _detect_mime(data) is None


# --- DEFAULT_COVER_REGEX --------------------------------------------------

@pytest.mark.parametrize("name", [
    "cover.jpg", "cover.jpeg", "cover.png", "cover.gif", "cover.webp",
    "cover.bmp", "Cover.JPG", "album.png", "folder.jpg", ".folder.jpg",
    "front.jpeg",
])
def test_default_cover_regex_matches(name: str) -> None:
    from mpd2mpris.cover import DEFAULT_COVER_REGEX
    assert DEFAULT_COVER_REGEX.match(name)


@pytest.mark.parametrize("name", [
    "song.flac", "readme.txt", "cover.txt", "back.jpg", "cover.tiff",
])
def test_default_cover_regex_rejects(name: str) -> None:
    from mpd2mpris.cover import DEFAULT_COVER_REGEX
    assert not DEFAULT_COVER_REGEX.match(name)


# --- _has_uri_scheme ------------------------------------------------------

@pytest.mark.parametrize("s", [
    "http://x", "https://x", "cdda://Disc1", "file:///x",
])
def test_has_uri_scheme_authority_form(s: str) -> None:
    """Only ``scheme://`` (authority-style) URIs trip the check; that's
    what callers want — readpicture stalls on those but not on plain
    relative MPD paths."""
    assert _has_uri_scheme(s)


@pytest.mark.parametrize("s", [
    "Artist/Song.flac", "/abs/path/song.flac",
    "", "no_scheme_here", "ftp_no_colon_slash",
    # local:track:... is the mopidy convention; lacks "//" so the
    # check returns False and step 1 will still try readpicture.
    "local:track:Artist/Song.flac",
])
def test_has_uri_scheme_false(s: str) -> None:
    assert not _has_uri_scheme(s)


# --- _is_virtual_cue_track ------------------------------------------------

@pytest.mark.parametrize("s", [
    "Artist/playlist.cue/track0001",
    ".disc-cuer/9c0bf40c/playlist.cue/track0001",
    "GrosseRadioReggae/playlist.cue/track0001",
    # case-insensitive
    "Artist/PLAYLIST.CUE/track0001",
    # tail digits aren't fixed-width
    "dir/sheet.cue/track1",
    "dir/sheet.cue/track99999",
])
def test_is_virtual_cue_track_true(s: str) -> None:
    """Matches MPD's ``sheet.cue/trackNNNN`` virtual-track shape — the
    marker we use to bypass readpicture/albumart (they fail on these)
    and derive the cue dir from the path."""
    assert _is_virtual_cue_track(s)


@pytest.mark.parametrize("s", [
    # plain audio files
    "Artist/Album/track.flac",
    "Artist/Album/track.mp3",
    # the .cue sheet itself, not a virtual track inside it
    "Artist/playlist.cue",
    # URI schemes — handled by ``_has_uri_scheme`` instead
    "cdda:///1",
    "http://example.com/stream.mp3",
    # embedded-CUE containers (.flac/.ape/.wv): out of scope for now,
    # the helper deliberately matches only ``.cue/trackNNNN``
    "Artist/album.flac/track01",
    # non-track suffix
    "Artist/playlist.cue/cover.jpg",
    # empty / no slash
    "",
    "playlist.cue",
])
def test_is_virtual_cue_track_false(s: str) -> None:
    assert not _is_virtual_cue_track(s)


# --- CoverFinder constructor + setters -----------------------------------

def test_default_capabilities_off() -> None:
    cf = CoverFinder()
    assert cf._can_readpicture is False
    assert cf._can_albumart is False


def test_update_capabilities() -> None:
    cf = CoverFinder()
    cf.update_capabilities(can_readpicture=True, can_albumart=False)
    assert cf._can_readpicture is True
    assert cf._can_albumart is False


def test_update_music_dir_round_trip() -> None:
    cf = CoverFinder()
    cf.update_music_dir(Path("/srv/music"))
    assert cf._music_dir == Path("/srv/music")
    cf.update_music_dir(None)
    assert cf._music_dir is None


# --- _song_path ----------------------------------------------------------

def test_song_path_file_uri() -> None:
    cf = CoverFinder()
    assert cf._song_path("file:///srv/music/x.flac") == Path("/srv/music/x.flac")


def test_song_path_file_uri_url_decoded() -> None:
    # ``Path.as_uri()`` URL-encodes spaces / accents — ``_song_path`` must
    # reverse it, otherwise ``Path(...).is_dir()`` short-circuits and
    # ``_scan_song_dir`` silently misses the cover.
    cf = CoverFinder()
    p = cf._song_path("file:///srv/music/Some%20Album/Song%20%231.flac")
    assert p == Path("/srv/music/Some Album/Song #1.flac")


def test_song_path_local_track_with_music_dir() -> None:
    cf = CoverFinder(CoverFinderConfig(music_dir=Path("/srv/music")))
    p = cf._song_path("local:track:Artist/Song.flac")
    assert p == Path("/srv/music/Artist/Song.flac")


def test_song_path_local_track_url_decoded() -> None:
    cf = CoverFinder(CoverFinderConfig(music_dir=Path("/srv/music")))
    p = cf._song_path("local:track:Artist/Song%20%231.flac")
    assert p == Path("/srv/music/Artist/Song #1.flac")


def test_song_path_local_track_without_music_dir() -> None:
    cf = CoverFinder()
    assert cf._song_path("local:track:Artist/Song.flac") is None


def test_song_path_other_scheme() -> None:
    cf = CoverFinder(CoverFinderConfig(music_dir=Path("/srv/music")))
    assert cf._song_path("http://stream.example/live.mp3") is None
    assert cf._song_path("cdda://Disc/Track01") is None


# --- _scan_song_dir ------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_song_dir_matches_cover_jpg(tmp_path: Path) -> None:
    (tmp_path / "cover.jpg").touch()
    (tmp_path / "song.flac").touch()
    cf = CoverFinder()
    assert await cf._scan_song_dir(tmp_path) == (tmp_path / "cover.jpg").as_uri()


@pytest.mark.asyncio
async def test_scan_song_dir_matches_folder_png(tmp_path: Path) -> None:
    (tmp_path / "folder.png").touch()
    cf = CoverFinder()
    assert await cf._scan_song_dir(tmp_path) == (tmp_path / "folder.png").as_uri()


@pytest.mark.asyncio
async def test_scan_song_dir_no_match(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").touch()
    cf = CoverFinder()
    assert await cf._scan_song_dir(tmp_path) is None


@pytest.mark.asyncio
async def test_scan_song_dir_none() -> None:
    cf = CoverFinder()
    assert await cf._scan_song_dir(None) is None


@pytest.mark.asyncio
async def test_scan_song_dir_nonexistent(tmp_path: Path) -> None:
    cf = CoverFinder()
    assert await cf._scan_song_dir(tmp_path / "does_not_exist") is None


@pytest.mark.asyncio
async def test_scan_song_dir_url_encodes_filename(tmp_path: Path) -> None:
    (tmp_path / "cover with space.jpg").touch()
    cf = CoverFinder()
    result = await cf._scan_song_dir(tmp_path)
    assert result is not None
    assert "cover%20with%20space.jpg" in result


@pytest.mark.asyncio
async def test_scan_song_dir_deterministic_on_multiple_matches(
    tmp_path: Path,
) -> None:
    # iterdir() ordering is filesystem-dependent; the scan must pick
    # the same file on every run regardless of creation order.
    for name in ("front.jpg", "album.png", "cover.jpg", "folder.png"):
        (tmp_path / name).touch()
    cf = CoverFinder()
    result = await cf._scan_song_dir(tmp_path)
    assert result == (tmp_path / "album.png").as_uri()


@pytest.mark.asyncio
async def test_scan_song_dir_swallows_oserror(
    tmp_path: Path, monkeypatch,
) -> None:
    # TOCTOU: dir vanishes between is_dir() and iterdir(); the scan
    # must log+return None rather than bubble up.
    def _raise(self) -> None:
        raise PermissionError(13, "denied")
    monkeypatch.setattr(Path, "iterdir", _raise)
    cf = CoverFinder()
    assert await cf._scan_song_dir(tmp_path) is None


# --- remote cover URL: tagged (_remote_cover) + title (_remote_cover_for_title)

def _async_return(value: object):
    async def _fn(*_a: object, **_k: object) -> object:
        return value
    return _fn


def _patch_track_sources(monkeypatch, mb=None, it=None, dz=None) -> None:
    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_for_track", _async_return(mb))
    monkeypatch.setattr("mpd2mpris.cover.itunes.cover_for_track", _async_return(it))
    monkeypatch.setattr("mpd2mpris.cover.deezer.cover_for_track", _async_return(dz))


@pytest.mark.asyncio
async def test_cover_for_title_unparseable_skips_query(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_for_track",
                        lambda *a: calls.append(a))
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover_for_title("bare station name") is None
    assert calls == []  # no separator → no source queried


@pytest.mark.asyncio
async def test_cover_for_title_musicbrainz_wins(monkeypatch) -> None:
    _patch_track_sources(monkeypatch, mb=_CAA, dz=_ITU)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz", "deezer")))
    assert await cf._remote_cover_for_title("Mato - 1980 Dub") == _CAA


@pytest.mark.asyncio
async def test_cover_for_title_falls_back_to_deezer(monkeypatch) -> None:
    # MusicBrainz resolves the wrong album (no CAA cover); Deezer finds the
    # right one within its own catalogue and wins.
    _patch_track_sources(monkeypatch, mb=None, dz=_ITU)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz", "deezer")))
    assert await cf._remote_cover_for_title("Elliott Smith - Waltz #2") == _ITU


@pytest.mark.asyncio
async def test_cover_for_title_skips_disabled_fallbacks(monkeypatch) -> None:
    # Deezer off: an MB miss isn't widened to it.
    _patch_track_sources(monkeypatch, mb=None, dz=_ITU)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover_for_title("Elliott Smith - Waltz #2") is None


@pytest.mark.asyncio
async def test_cover_for_title_memoises(monkeypatch) -> None:
    calls: list = []

    async def _mb(artist: str, track: str) -> str:
        calls.append((artist, track))
        return _CAA

    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_for_track", _mb)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover_for_title("Mato - 1980 Dub") == _CAA
    assert await cf._remote_cover_for_title("Mato - 1980 Dub") == _CAA
    assert calls == [("Mato", "1980 Dub")]  # second served from cache


@pytest.mark.asyncio
async def test_cover_for_title_not_cached_on_transient_error(monkeypatch) -> None:
    calls: list = []

    async def _mb(artist: str, track: str) -> str:
        calls.append((artist, track))
        if len(calls) == 1:
            raise OSError("transient")
        return _CAA

    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_for_track", _mb)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover_for_title("Mato - 1980 Dub") is None  # errored → not cached
    assert await cf._remote_cover_for_title("Mato - 1980 Dub") == _CAA  # retried
    assert calls == [("Mato", "1980 Dub"), ("Mato", "1980 Dub")]


def _patch_sources(monkeypatch, mb=None, it=None, dz=None) -> None:
    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_url", _async_return(mb))
    monkeypatch.setattr("mpd2mpris.cover.itunes.cover_url", _async_return(it))
    monkeypatch.setattr("mpd2mpris.cover.deezer.cover_url", _async_return(dz))


_CAA = "https://coverartarchive.org/release/rel-1/front-500.jpg"
_ITU = "https://is1.mzstatic.com/image/.../600x600bb.jpg"


@pytest.mark.asyncio
async def test_remote_cover_returns_first_url(monkeypatch) -> None:
    _patch_sources(monkeypatch, mb=_CAA)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover("A", "B") == _CAA


@pytest.mark.asyncio
async def test_remote_cover_falls_back_to_next_source(monkeypatch) -> None:
    # MusicBrainz/CAA has nothing; iTunes (opt-in) provides the URL.
    _patch_sources(monkeypatch, mb=None, it=_ITU)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz", "itunes")))
    assert await cf._remote_cover("A", "B") == _ITU


@pytest.mark.asyncio
async def test_remote_cover_short_circuits_after_first_hit(monkeypatch) -> None:
    # A higher-priority hit skips the lower-priority sources entirely (no wasted
    # API calls) — lookups run sequentially in priority order, not concurrently.
    it_calls: list = []

    async def _it(artist: str, album: str) -> str:
        it_calls.append((artist, album))
        return _ITU

    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_url", _async_return(_CAA))
    monkeypatch.setattr("mpd2mpris.cover.itunes.cover_url", _it)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz", "itunes")))
    assert await cf._remote_cover("A", "B") == _CAA
    assert it_calls == []  # itunes never queried — musicbrainz already answered


@pytest.mark.asyncio
async def test_remote_cover_skips_disabled_fallbacks(monkeypatch) -> None:
    # iTunes/Deezer off: an MB miss isn't widened to them.
    _patch_sources(monkeypatch, mb=None, it=_ITU, dz=_ITU)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover("A", "B") is None


@pytest.mark.asyncio
async def test_remote_cover_none_when_no_source_has_it(monkeypatch) -> None:
    _patch_sources(monkeypatch)  # all None
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover("A", "B") is None


@pytest.mark.asyncio
async def test_remote_cover_no_sources_enabled(monkeypatch) -> None:
    # Default config: no step-5 source enabled → no query, clean None.
    _patch_sources(monkeypatch, mb=_CAA)
    cf = CoverFinder()
    assert await cf._remote_cover("A", "B") is None


@pytest.mark.asyncio
async def test_remote_cover_memoises(monkeypatch) -> None:
    calls: list = []

    async def _mb(artist: str, album: str) -> str:
        calls.append((artist, album))
        return _CAA

    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_url", _mb)
    monkeypatch.setattr("mpd2mpris.cover.itunes.cover_url", _async_return(None))
    monkeypatch.setattr("mpd2mpris.cover.deezer.cover_url", _async_return(None))
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover("A", "B") == _CAA
    assert await cf._remote_cover("A", "B") == _CAA
    assert calls == [("A", "B")]  # second served from cache


@pytest.mark.asyncio
async def test_remote_cover_not_cached_on_transient_error(monkeypatch) -> None:
    # A source error must not poison the cache: the first lookup raises, the
    # second succeeds and yields the URL (i.e. it was retried, not cached None).
    calls: list = []

    async def _mb(artist: str, album: str) -> str:
        calls.append((artist, album))
        if len(calls) == 1:
            raise OSError("transient")
        return _CAA

    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_url", _mb)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover("A", "B") is None  # errored → not cached
    assert await cf._remote_cover("A", "B") == _CAA  # retried, now resolves
    assert calls == [("A", "B"), ("A", "B")]


@pytest.mark.asyncio
async def test_remote_cover_caches_confirmed_miss(monkeypatch) -> None:
    # A clean all-source miss IS cached (no error) — not re-queried.
    calls: list = []

    async def _mb(artist: str, album: str) -> None:
        calls.append((artist, album))
        return None

    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_url", _mb)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("musicbrainz",)))
    assert await cf._remote_cover("A", "B") is None
    assert await cf._remote_cover("A", "B") is None
    assert calls == [("A", "B")]  # confirmed miss cached


@pytest.mark.asyncio
async def test_remote_cover_order_follows_config(monkeypatch) -> None:
    # Priority follows the configured list order, not the registry order.
    monkeypatch.setattr("mpd2mpris.cover.musicbrainz.cover_url", _async_return("mb"))
    monkeypatch.setattr("mpd2mpris.cover.deezer.cover_url", _async_return("dz"))
    cf = CoverFinder(CoverFinderConfig(cover_sources=("deezer", "musicbrainz")))
    assert await cf._remote_cover("A", "B") == "dz"


@pytest.mark.asyncio
async def test_remote_cover_unknown_source_ignored(monkeypatch) -> None:
    _patch_sources(monkeypatch, mb=_CAA)
    cf = CoverFinder(CoverFinderConfig(cover_sources=("bogus", "musicbrainz")))
    assert await cf._remote_cover("A", "B") == _CAA  # bogus dropped, mb used


_WDB = "https://jcorporation.github.io/webradiodb/db/pics/stream.webp"


# --- _stream_cover (steps 6-7: radiobrowser + myMPD, one priority list) ---

@pytest.mark.asyncio
async def test_stream_cover_skips_non_http(monkeypatch) -> None:
    calls: list[str] = []

    async def _icon(url: str) -> str:
        calls.append(url)
        return "https://x/favicon.ico"

    monkeypatch.setattr("mpd2mpris.cover.radiobrowser.station_icon", _icon)
    cf = CoverFinder(CoverFinderConfig(stream_sources=("radiobrowser",)))
    assert await cf._stream_cover("relative/track.flac") is None
    assert calls == []  # not an http(s) stream → never queried


@pytest.mark.asyncio
async def test_stream_cover_returns_url_and_memoises(monkeypatch) -> None:
    calls: list[str] = []

    async def _icon(url: str) -> str:
        calls.append(url)
        return "https://x/favicon.ico"

    monkeypatch.setattr("mpd2mpris.cover.radiobrowser.station_icon", _icon)
    cf = CoverFinder(CoverFinderConfig(stream_sources=("radiobrowser",)))
    stream = "http://hd.example.info/reggae-192.mp3"
    assert await cf._stream_cover(stream) == "https://x/favicon.ico"
    assert await cf._stream_cover(stream) == "https://x/favicon.ico"
    assert calls == [stream]  # second call served from memo


@pytest.mark.asyncio
async def test_stream_cover_not_cached_on_transient_error(monkeypatch) -> None:
    calls: list[str] = []

    async def _icon(url: str) -> str:
        calls.append(url)
        if len(calls) == 1:
            raise OSError("transient")
        return "https://x/favicon.ico"

    monkeypatch.setattr("mpd2mpris.cover.radiobrowser.station_icon", _icon)
    cf = CoverFinder(CoverFinderConfig(stream_sources=("radiobrowser",)))
    stream = "http://hd.example.info/reggae-192.mp3"
    assert await cf._stream_cover(stream) is None  # errored → not cached
    assert await cf._stream_cover(stream) == "https://x/favicon.ico"  # retried


@pytest.mark.asyncio
async def test_stream_cover_mympd_passes_configured_base_url(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def _cover(base: str, stream: str) -> str:
        calls.append((base, stream))
        return _WDB

    monkeypatch.setattr("mpd2mpris.cover.mympd.cover_url", _cover)
    cf = CoverFinder(CoverFinderConfig(stream_sources=("mympd",), mympd_url="http://host:8080"))
    stream = "http://absolut.example/coffee.mp3"
    assert await cf._stream_cover(stream) == _WDB
    assert calls == [("http://host:8080", stream)]  # builder closed over the base URL


@pytest.mark.asyncio
async def test_stream_cover_default_off(monkeypatch) -> None:
    monkeypatch.setattr("mpd2mpris.cover.radiobrowser.station_icon", _async_return("https://x/fav.ico"))
    cf = CoverFinder()  # no stream_sources
    assert await cf._stream_cover("http://stream") is None


@pytest.mark.asyncio
async def test_stream_cover_order_follows_config(monkeypatch) -> None:
    monkeypatch.setattr("mpd2mpris.cover.radiobrowser.station_icon", _async_return("https://x/fav.ico"))
    monkeypatch.setattr("mpd2mpris.cover.mympd.cover_url", _async_return(_WDB))
    # radiobrowser listed first → it wins over myMPD this time.
    cf = CoverFinder(CoverFinderConfig(
        stream_sources=("radiobrowser", "mympd"), mympd_url="http://host:8080",
    ))
    assert await cf._stream_cover("http://stream") == "https://x/fav.ico"


@pytest.mark.asyncio
async def test_stream_cover_mympd_listed_without_uri_skipped(monkeypatch) -> None:
    monkeypatch.setattr("mpd2mpris.cover.mympd.cover_url", _async_return(_WDB))
    cf = CoverFinder(CoverFinderConfig(stream_sources=("mympd",)))  # no mympd_url
    assert await cf._stream_cover("http://stream") is None  # mympd skipped at init


@pytest.mark.asyncio
async def test_stream_cover_unknown_source_ignored(monkeypatch) -> None:
    monkeypatch.setattr("mpd2mpris.cover.radiobrowser.station_icon", _async_return("https://x/fav.ico"))
    cf = CoverFinder(CoverFinderConfig(stream_sources=("bogus", "radiobrowser")))
    assert await cf._stream_cover("http://stream") == "https://x/fav.ico"


# --- _materialise + temp reuse via find() --------------------------------

def test_materialise_writes_bytes_at_returned_uri() -> None:
    cf = CoverFinder()
    uri = cf._materialise("file:///srv/music/x.flac", b"PNGDATA", "image/png")
    assert uri.startswith("file://")
    path = Path(uri[7:])
    assert path.exists()
    assert path.suffix == ".png"
    assert path.read_bytes() == b"PNGDATA"
    cf._discard_temp()
    assert not path.exists()


def test_materialise_uses_jpg_for_unknown_mime() -> None:
    cf = CoverFinder()
    uri = cf._materialise("file:///x", b"raw", "image/x-weird")
    assert uri.endswith(".jpg")
    cf._discard_temp()


def test_discard_temp_no_op_when_empty() -> None:
    cf = CoverFinder()
    cf._discard_temp()  # should not raise even when nothing is held
    assert cf._temp_cover is None
    assert cf._temp_song_uri is None


# --- find() orchestration with mocked MPD client -------------------------

def _client_with(
    readpicture=None, albumart=None, find=None,
) -> MagicMock:
    """Build a MagicMock client where the named coros return the given
    payloads. Each is AsyncMock so ``await`` works. ``__name__`` is set
    on each so the cover-finder code can introspect it (it derives the
    matching capability flag from the method name)."""
    c = MagicMock()
    for name, payload in (
        ("readpicture", readpicture or {}),
        ("albumart", albumart or {}),
    ):
        mock = AsyncMock(return_value=payload)
        mock.__name__ = name
        setattr(c, name, mock)
    c.find = AsyncMock(return_value=find or [])
    return c


@pytest.mark.asyncio
async def test_find_step1_returns_mpd_readpicture_cover() -> None:
    cf = CoverFinder(CoverFinderConfig(can_readpicture=True))
    client = _client_with(readpicture={"binary": b"\xff\xd8JPEGDATA"})
    uri = await cf.find(SongLookup(
        client=client,
        song_uri="file:///srv/music/Song.flac",
        song_file="Song.flac",
        mpd_meta={},
    ))
    assert uri is not None
    assert uri.startswith("file://")
    path = Path(uri[7:])
    assert path.read_bytes().startswith(b"\xff\xd8")
    cf._discard_temp()


@pytest.mark.asyncio
async def test_find_step1_skipped_for_uri_scheme() -> None:
    """song_file with a URI scheme (cdda://, http://) must NOT trigger
    readpicture — it stalls the MPD connection (commit 234d6da)."""
    cf = CoverFinder(CoverFinderConfig(can_readpicture=True))
    client = _client_with(readpicture={"binary": b"\xff\xd8X"})
    await cf.find(SongLookup(
        client=client,
        song_uri="cdda://Disc1/Track01",
        song_file="cdda://Disc1/Track01",
        mpd_meta={},
    ))
    client.readpicture.assert_not_called()


@pytest.mark.asyncio
async def test_find_falls_through_to_step3_filesystem(
    tmp_path: Path,
) -> None:
    """No MPD readpicture (caps off) — falls through to the FS scan
    which finds cover.jpg directly (no tempfile)."""
    song_dir = tmp_path / "Artist" / "Album"
    song_dir.mkdir(parents=True)
    (song_dir / "cover.jpg").touch()
    song_path = song_dir / "song.flac"
    song_path.touch()

    cf = CoverFinder(CoverFinderConfig(
        music_dir=tmp_path, can_readpicture=False, can_albumart=False,
    ))
    uri = await cf.find(SongLookup(
        client=_client_with(),
        song_uri=song_path.as_uri(),
        song_file=str(song_path.relative_to(tmp_path)),
        mpd_meta={},
    ))
    assert uri == (song_dir / "cover.jpg").as_uri()


@pytest.mark.asyncio
async def test_find_falls_through_to_step5_remote_url(
    tmp_path: Path, monkeypatch,
) -> None:
    _patch_sources(monkeypatch, mb="https://caa/front-500.jpg")
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    cf = CoverFinder(CoverFinderConfig(music_dir=music_dir, cover_sources=("musicbrainz",)))
    uri = await cf.find(SongLookup(
        client=_client_with(),
        song_uri=(music_dir / "Song.flac").as_uri(),
        song_file="Song.flac",
        mpd_meta={"artist": "Artist", "album": "Album"},
    ))
    assert uri == "https://caa/front-500.jpg"  # remote URL returned as-is, not downloaded


@pytest.mark.asyncio
async def test_find_prefers_mympd_cover_over_favicon(
    tmp_path: Path, monkeypatch,
) -> None:
    # Steps 1-5 miss (web-radio stream, no key); both stream sources resolve
    # — myMPD is listed first, so its curated cover wins.
    _patch_sources(monkeypatch)
    monkeypatch.setattr("mpd2mpris.cover.radiobrowser.station_icon", _async_return("https://x/favicon.ico"))
    monkeypatch.setattr("mpd2mpris.cover.mympd.cover_url", _async_return(_WDB))
    cf = CoverFinder(CoverFinderConfig(
        stream_sources=("mympd", "radiobrowser"), mympd_url="http://host:8080",
    ))
    uri = await cf.find(SongLookup(
        client=_client_with(),
        song_uri="http://stream/radio",
        song_file="http://stream/radio",
        mpd_meta={"title": "Jingle"},  # title-only, no resolvable key
    ))
    assert uri == _WDB


@pytest.mark.asyncio
async def test_find_falls_back_to_favicon_without_mympd(
    tmp_path: Path, monkeypatch,
) -> None:
    # myMPD listed but no mympd_url (skipped) — the favicon still serves.
    _patch_sources(monkeypatch)
    monkeypatch.setattr("mpd2mpris.cover.radiobrowser.station_icon", _async_return("https://x/favicon.ico"))
    cf = CoverFinder(CoverFinderConfig(stream_sources=("mympd", "radiobrowser")))  # no mympd_url
    uri = await cf.find(SongLookup(
        client=_client_with(),
        song_uri="http://stream/radio",
        song_file="http://stream/radio",
        mpd_meta={"title": "Jingle"},
    ))
    assert uri == "https://x/favicon.ico"


@pytest.mark.asyncio
async def test_find_returns_none_when_nothing_matches(
    tmp_path: Path, monkeypatch,
) -> None:
    _patch_sources(monkeypatch)  # no remote cover from any source
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    cf = CoverFinder(CoverFinderConfig(music_dir=music_dir))
    uri = await cf.find(SongLookup(
        client=_client_with(),
        song_uri=(music_dir / "Nope.flac").as_uri(),
        song_file="Nope.flac",
        mpd_meta={},
    ))
    assert uri is None


@pytest.mark.asyncio
async def test_find_reuses_temp_for_same_song_uri() -> None:
    cf = CoverFinder(CoverFinderConfig(can_readpicture=True))
    client = _client_with(readpicture={"binary": b"\xff\xd8data1"})
    req = SongLookup(
        client=client, song_uri="file:///x.flac", song_file="x.flac", mpd_meta={},
    )
    uri1 = await cf.find(req)
    # second call shouldn't touch MPD again
    client.readpicture.reset_mock()
    uri2 = await cf.find(req)
    assert uri1 == uri2
    client.readpicture.assert_not_called()
    cf._discard_temp()


@pytest.mark.asyncio
async def test_find_discards_temp_when_song_uri_changes() -> None:
    cf = CoverFinder(CoverFinderConfig(can_readpicture=True))
    client = _client_with(readpicture={"binary": b"\xff\xd8first"})
    uri1 = await cf.find(SongLookup(
        client=client, song_uri="file:///a.flac", song_file="a.flac", mpd_meta={},
    ))
    # change the cover payload so we can distinguish
    client.readpicture = AsyncMock(return_value={"binary": b"\xff\xd8second"})
    client.readpicture.__name__ = "readpicture"
    uri2 = await cf.find(SongLookup(
        client=client, song_uri="file:///b.flac", song_file="b.flac", mpd_meta={},
    ))
    assert uri1 != uri2
    # first file should be gone
    assert not Path(uri1[7:]).exists()
    cf._discard_temp()


@pytest.mark.asyncio
async def test_find_unknown_mime_skips_cover() -> None:
    """MPD returned bytes we can't identify — better skip than serve
    garbage as JPEG."""
    cf = CoverFinder(CoverFinderConfig(can_readpicture=True))
    client = _client_with(readpicture={"binary": b"\x00\x01\x02\x03not_an_image"})
    uri = await cf.find(SongLookup(
        client=client, song_uri="file:///x.flac", song_file="x.flac", mpd_meta={},
    ))
    assert uri is None


# --- _cue_dir_from_playlist / _cue_dir_from_song_file ------------------

def test_cue_dir_from_playlist_strips_music_dir_prefix() -> None:
    cf = CoverFinder(CoverFinderConfig(music_dir=Path("/srv/music")))
    assert cf._cue_dir_from_playlist("/srv/music/Artist/album.cue") == Path("Artist")


def test_cue_dir_from_playlist_relative_path() -> None:
    cf = CoverFinder(CoverFinderConfig(music_dir=Path("/srv/music")))
    assert cf._cue_dir_from_playlist("Artist/album.cue") == Path("Artist")


def test_cue_dir_from_playlist_empty_returns_none() -> None:
    cf = CoverFinder(CoverFinderConfig())
    assert cf._cue_dir_from_playlist("") is None


def test_cue_dir_from_playlist_top_level_returns_none() -> None:
    # "album.cue" has no parent dir under music_dir — nothing to scan.
    cf = CoverFinder(CoverFinderConfig(music_dir=Path("/srv/music")))
    assert cf._cue_dir_from_playlist("/srv/music/album.cue") is None


def test_cue_dir_from_song_file_uses_grandparent() -> None:
    cf = CoverFinder(CoverFinderConfig())
    assert cf._cue_dir_from_song_file(
        "Artist/playlist.cue/track0001"
    ) == Path("Artist")


def test_cue_dir_from_song_file_regular_track_returns_none() -> None:
    # Regular track ("Artist/Album/track.flac") — not a CUE virtual
    # track, leave it for the normal step 1/2/3.
    cf = CoverFinder(CoverFinderConfig())
    assert cf._cue_dir_from_song_file("Artist/Album/track.flac") is None


def test_cue_dir_from_song_file_uri_scheme_returns_none() -> None:
    cf = CoverFinder(CoverFinderConfig())
    assert cf._cue_dir_from_song_file("cdda:///1") is None


def test_cue_dir_from_song_file_works_without_music_dir() -> None:
    # The ``.cue/trackNNNN`` shape is a reliable marker — no need to
    # stat the filesystem, so the fallback works even when the user
    # hasn't configured ``music_dir``.
    cf = CoverFinder(CoverFinderConfig())
    assert cf._cue_dir_from_song_file(
        "GrosseRadioReggae/playlist.cue/track0001"
    ) == Path("GrosseRadioReggae")


def test_cue_dir_from_song_file_top_level_container_returns_none() -> None:
    # "playlist.cue/track0001" — grandparent is "." → nothing to scan.
    cf = CoverFinder(CoverFinderConfig())
    assert cf._cue_dir_from_song_file("playlist.cue/track0001") is None


# --- _cue_fallback (CUE/cdda fallback) ----------------------------------

@pytest.mark.asyncio
async def test_cue_fallback_fs_scan_short_circuits_albumart(tmp_path: Path) -> None:
    # CUE on local FS with a regex-matched cover next to it → FS scan
    # returns the file URI directly, no MPD albumart round-trip and no
    # /tmp copy.
    cue_dir = tmp_path / ".disc-cuer/abc"
    cue_dir.mkdir(parents=True)
    (cue_dir / "folder.jpg").touch()
    cf = CoverFinder(CoverFinderConfig(
        music_dir=tmp_path, can_albumart=True,
    ))
    client = _client_with(albumart={"binary": b"\xff\xd8JPEG"})
    uri = await cf._cue_fallback(SongLookup(
        client=client,
        song_uri="cdda://Disc1/Track01",
        song_file="cdda:///1",
        mpd_meta={"track": "1"},
        last_loaded_playlist=str(cue_dir / "playlist.cue"),
    ))
    assert uri == (cue_dir / "folder.jpg").as_uri()
    client.albumart.assert_not_awaited()


@pytest.mark.asyncio
async def test_cue_fallback_albumart_in_cue_dir() -> None:
    cf = CoverFinder(CoverFinderConfig(
        music_dir=Path("/srv/music"), can_albumart=True,
    ))
    client = _client_with(albumart={"binary": b"\xff\xd8JPEG"})
    uri = await cf._cue_fallback(SongLookup(
        client=client,
        song_uri="cdda://Disc1/Track01",
        song_file="cdda:///1",
        mpd_meta={"track": "1"},
        last_loaded_playlist="/srv/music/.disc-cuer/abc/playlist.cue",
    ))
    assert uri is not None
    # Exactly one albumart call, in the CUE's parent dir — MPD's
    # albumart command resolves cover.{png,jpg,jxl,webp} server-side,
    # so the path-suffix we pass is just a directory hint.
    client.albumart.assert_awaited_once()
    queried = client.albumart.await_args_list[0].args[0]
    assert queried.startswith(".disc-cuer/abc/")
    assert "playlist.cue/" not in queried


@pytest.mark.asyncio
async def test_cue_fallback_returns_none_without_playlist() -> None:
    cf = CoverFinder(CoverFinderConfig(can_albumart=True))
    uri = await cf._cue_fallback(SongLookup(
        client=_client_with(albumart={"binary": b"x"}),
        song_uri="cdda://Disc1/Track01",
        song_file="cdda:///1",
        mpd_meta={"track": "1"},
        last_loaded_playlist="",
    ))
    assert uri is None


@pytest.mark.asyncio
async def test_cue_fallback_infers_from_song_file_when_playlist_empty(
    tmp_path: Path,
) -> None:
    # MPD only fills ``lastloadedplaylist`` when the CUE was added via
    # ``load`` — adding it through ``add`` leaves the field empty.
    # Derive the cue dir from ``song_file`` itself: a virtual track
    # ``dir/sheet.cue/trackNNNN`` means the grandparent holds the
    # cover. With music_dir set, the FS scan short-circuits albumart.
    album_dir = tmp_path / "GrosseRadioReggae"
    album_dir.mkdir()
    (album_dir / "cover.png").touch()
    cf = CoverFinder(CoverFinderConfig(music_dir=tmp_path, can_albumart=True))
    client = _client_with(albumart={"binary": b"\xff\xd8JPEG"})
    uri = await cf._cue_fallback(SongLookup(
        client=client,
        song_uri=(album_dir / "playlist.cue/track0001").as_uri(),
        song_file="GrosseRadioReggae/playlist.cue/track0001",
        mpd_meta={"title": "Track 1"},
        last_loaded_playlist="",
    ))
    assert uri == (album_dir / "cover.png").as_uri()
    client.albumart.assert_not_awaited()


@pytest.mark.asyncio
async def test_cue_fallback_infers_from_song_file_without_music_dir() -> None:
    # Real-world case: user has no music_dir configured and adds a CUE
    # via ``add`` (so lastloadedplaylist is empty too). We still want
    # the albumart call against the cue dir to fire — that's the only
    # way the cover surfaces.
    cf = CoverFinder(CoverFinderConfig(can_albumart=True))
    client = _client_with(albumart={"binary": b"\xff\xd8JPEG"})
    uri = await cf._cue_fallback(SongLookup(
        client=client,
        song_uri="file:///irrelevant",
        song_file="GrosseRadioReggae/playlist.cue/track0001",
        mpd_meta={"title": "Track 1"},
        last_loaded_playlist="",
    ))
    assert uri is not None
    client.albumart.assert_awaited_once()
    queried = client.albumart.await_args_list[0].args[0]
    assert queried.startswith("GrosseRadioReggae/")
    assert "playlist.cue/" not in queried


@pytest.mark.asyncio
async def test_cue_fallback_returns_none_when_no_cover() -> None:
    cf = CoverFinder(CoverFinderConfig(
        music_dir=Path("/srv/music"), can_albumart=True,
    ))
    uri = await cf._cue_fallback(SongLookup(
        client=_client_with(albumart={}),
        song_uri="cdda://Disc1/Track01",
        song_file="cdda:///1",
        mpd_meta={"track": "1"},
        last_loaded_playlist="/srv/music/.disc-cuer/abc/playlist.cue",
    ))
    assert uri is None


