"""Unit tests for cover.py — pure helpers, filesystem steps, and the
async ``find`` orchestration with a stubbed MPD client.

Mutagen extraction (step 2) is not exercised here: it would need a
real media file with embedded art per format. Covered by integration.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mpdris2.cover import (
    CoverFinder,
    CoverFinderConfig,
    SongLookup,
    _detect_mime,
    _has_uri_scheme,
)

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
    from mpdris2.cover import DEFAULT_COVER_REGEX
    assert DEFAULT_COVER_REGEX.match(name)


@pytest.mark.parametrize("name", [
    "song.flac", "readme.txt", "cover.txt", "back.jpg", "cover.tiff",
])
def test_default_cover_regex_rejects(name: str) -> None:
    from mpdris2.cover import DEFAULT_COVER_REGEX
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


# --- _lookup_downloads_cache ---------------------------------------------

def test_lookup_downloads_cache_hit(tmp_path: Path) -> None:
    (tmp_path / "Artist-Album.jpg").touch()
    cf = CoverFinder(CoverFinderConfig(cover_cache_dir=tmp_path))
    result = cf._lookup_downloads_cache({"artist": "Artist", "album": "Album"})
    assert result == (tmp_path / "Artist-Album.jpg").as_uri()


def test_lookup_downloads_cache_miss(tmp_path: Path) -> None:
    cf = CoverFinder(CoverFinderConfig(cover_cache_dir=tmp_path))
    result = cf._lookup_downloads_cache({"artist": "Artist", "album": "Album"})
    assert result is None


def test_lookup_downloads_cache_missing_artist(tmp_path: Path) -> None:
    cf = CoverFinder(CoverFinderConfig(cover_cache_dir=tmp_path))
    assert cf._lookup_downloads_cache({"album": "Album"}) is None


def test_lookup_downloads_cache_missing_album(tmp_path: Path) -> None:
    cf = CoverFinder(CoverFinderConfig(cover_cache_dir=tmp_path))
    assert cf._lookup_downloads_cache({"artist": "Artist"}) is None


def test_lookup_downloads_cache_list_artist_uses_first(tmp_path: Path) -> None:
    (tmp_path / "A-B.jpg").touch()
    cf = CoverFinder(CoverFinderConfig(cover_cache_dir=tmp_path))
    result = cf._lookup_downloads_cache(
        {"artist": ["A", "X", "Y"], "album": "B"}
    )
    assert result == (tmp_path / "A-B.jpg").as_uri()


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
async def test_find_falls_through_to_step4_downloads_cache(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "Artist-Album.jpg").touch()
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    cf = CoverFinder(CoverFinderConfig(
        music_dir=music_dir, cover_cache_dir=cache_dir,
    ))
    uri = await cf.find(SongLookup(
        client=_client_with(),
        song_uri=(music_dir / "Song.flac").as_uri(),
        song_file="Song.flac",
        mpd_meta={"artist": "Artist", "album": "Album"},
    ))
    assert uri == (cache_dir / "Artist-Album.jpg").as_uri()


@pytest.mark.asyncio
async def test_find_returns_none_when_nothing_matches(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    cf = CoverFinder(CoverFinderConfig(
        music_dir=music_dir, cover_cache_dir=cache_dir,
    ))
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


