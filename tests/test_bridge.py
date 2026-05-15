"""Unit tests for bridge.py pure helpers + the ``_build_track_metadata``
method — no MPD, no D-Bus.

``_build_track_metadata`` runs on a partially-initialised
``MpdMprisBridge`` built via ``__new__`` (we skip the heavy ``__init__``
which needs a running event loop). Only the attributes the method
reads are set on it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mpdris2.bridge import (
    MpdMprisBridge,
    _is_external_seek,
    _loop_status_from,
    _parse_elapsed,
    _parse_volume,
    _playback_status_from,
    _resolve_song_url,
)


def _bridge(cover_finder, music_dir=Path("/srv/music"),
            url_handlers=("http://",), client=None):
    """Minimal bridge stub — only the fields ``_build_track_metadata``
    touches."""
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge.client = client or MagicMock()
    bridge.music_dir = music_dir
    bridge.url_handlers = list(url_handlers)
    bridge.cover_finder = cover_finder
    return bridge


# --- _playback_status_from -------------------------------------------------

@pytest.mark.parametrize("state,expected", [
    ("play", "Playing"),
    ("pause", "Paused"),
    ("stop", "Stopped"),
    ("", "Stopped"),
    ("garbage", "Stopped"),
])
def test_playback_status_from(state: str, expected: str) -> None:
    assert _playback_status_from(state) == expected


# --- _loop_status_from -----------------------------------------------------

@pytest.mark.parametrize("repeat,single,expected", [
    (False, False, "None"),
    (True, False, "Playlist"),
    (True, True, "Track"),
    (False, True, "None"),  # single without repeat doesn't loop
])
def test_loop_status_from(repeat: bool, single: bool, expected: str) -> None:
    assert _loop_status_from(repeat, single) == expected


# --- _parse_volume ---------------------------------------------------------

def test_parse_volume_valid() -> None:
    assert _parse_volume({"volume": "75"}) == 0.75


def test_parse_volume_zero() -> None:
    assert _parse_volume({"volume": "0"}) == 0.0


def test_parse_volume_missing_means_no_change() -> None:
    assert _parse_volume({}) is None


def test_parse_volume_minus_one_means_unreportable() -> None:
    # MPD returns -1 when the audio backend can't report the level.
    assert _parse_volume({"volume": "-1"}) is None


def test_parse_volume_garbage_means_no_change() -> None:
    assert _parse_volume({"volume": "loud"}) is None


# --- _parse_elapsed --------------------------------------------------------

def test_parse_elapsed_valid() -> None:
    assert _parse_elapsed({"elapsed": "12.345"}) == 12.345


def test_parse_elapsed_missing() -> None:
    assert _parse_elapsed({}) == 0.0


def test_parse_elapsed_garbage() -> None:
    assert _parse_elapsed({"elapsed": "n/a"}) == 0.0


# --- _is_external_seek -----------------------------------------------------

def test_seek_within_tolerance_is_not_external() -> None:
    # 10s ago elapsed=5.0, now=15s wall-clock, observed=15.0 → expected=15.0
    assert not _is_external_seek({"elapsed": "5.0"}, 0.0, 15.0, 10.0)


def test_seek_deviation_above_threshold_is_external() -> None:
    # 10s elapsed, but actual position jumped to 30s → external seek
    assert _is_external_seek({"elapsed": "5.0"}, 0.0, 30.0, 10.0)


def test_seek_deviation_at_threshold_is_not_external() -> None:
    # Exactly 0.6s deviation is the boundary; spec says > 0.6 only.
    assert not _is_external_seek({"elapsed": "5.0"}, 0.0, 15.6, 10.0)


def test_seek_deviation_just_above_threshold_is_external() -> None:
    assert _is_external_seek({"elapsed": "5.0"}, 0.0, 15.7, 10.0)


# --- _resolve_song_url -----------------------------------------------------

def test_resolve_song_url_relative_with_music_dir() -> None:
    song = {"file": "Artist/Album/Song.flac"}
    assert _resolve_song_url(song, Path("/srv/music"), ["http://"]) == (
        "file:///srv/music/Artist/Album/Song.flac"
    )


def test_resolve_song_url_http_passes_through() -> None:
    song = {"file": "http://stream.example/live.mp3"}
    assert _resolve_song_url(song, Path("/srv/music"), ["http://"]) == (
        "http://stream.example/live.mp3"
    )


def test_resolve_song_url_no_music_dir_returns_raw() -> None:
    song = {"file": "Artist/Song.flac"}
    assert _resolve_song_url(song, None, ["http://"]) == "Artist/Song.flac"


def test_resolve_song_url_empty_song() -> None:
    assert _resolve_song_url({}, Path("/srv/music"), ["http://"]) == ""


def test_resolve_song_url_url_encodes_specials() -> None:
    song = {"file": "Artist/Album 01/Song #1.flac"}
    assert _resolve_song_url(song, Path("/srv/music"), ["http://"]) == (
        "file:///srv/music/Artist/Album%2001/Song%20%231.flac"
    )


# --- _build_track_metadata (async) ----------------------------------------

@pytest.mark.asyncio
async def test_build_track_metadata_no_song_url_skips_cover() -> None:
    """When the song has no file, cover_finder.find must NOT be called."""
    cover_finder = MagicMock()
    cover_finder.find = MagicMock(side_effect=AssertionError("should not be called"))
    bridge = _bridge(cover_finder)
    meta = await bridge._build_track_metadata(song={"title": "x"}, status={})
    assert "xesam:title" in meta
    assert "mpris:artUrl" not in meta


@pytest.mark.asyncio
async def test_build_track_metadata_cover_attached() -> None:
    async def fake_find(*args, **kwargs):
        return "file:///cache/cover.jpg"
    cover_finder = MagicMock()
    cover_finder.find = fake_find
    bridge = _bridge(cover_finder)
    meta = await bridge._build_track_metadata(
        song={"title": "x", "file": "Artist/Song.flac"}, status={},
    )
    assert meta["mpris:artUrl"].value == "file:///cache/cover.jpg"


@pytest.mark.asyncio
async def test_build_track_metadata_cover_exception_swallowed(caplog) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("cover lookup broke")
    cover_finder = MagicMock()
    cover_finder.find = boom
    bridge = _bridge(cover_finder)
    with caplog.at_level("ERROR"):
        meta = await bridge._build_track_metadata(
            song={"title": "x", "file": "Artist/Song.flac"}, status={},
        )
    assert "mpris:artUrl" not in meta
    assert "xesam:title" in meta
    assert any("cover lookup failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_build_track_metadata_cover_none_no_arturl() -> None:
    async def empty(*args, **kwargs):
        return None
    cover_finder = MagicMock()
    cover_finder.find = empty
    bridge = _bridge(cover_finder)
    meta = await bridge._build_track_metadata(
        song={"file": "Artist/Song.flac"}, status={},
    )
    assert "mpris:artUrl" not in meta


# --- _previous_cdaware -----------------------------------------------------

def _mpd_client_with_status(elapsed: float, songid: str = "7"):
    client = MagicMock()
    client.status = AsyncMock(return_value={"elapsed": str(elapsed), "songid": songid})
    client.previous = AsyncMock()
    client.seekid = AsyncMock()
    return client


def _bridge_with_cdprev(cdprev: bool) -> MpdMprisBridge:
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge._cdprev = cdprev
    return bridge


@pytest.mark.asyncio
async def test_previous_cdaware_disabled_always_previous() -> None:
    bridge = _bridge_with_cdprev(False)
    client = _mpd_client_with_status(elapsed=12.0)
    await bridge._previous_cdaware(client)
    client.previous.assert_awaited_once()
    client.seekid.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_cdaware_under_3s_skips_back() -> None:
    bridge = _bridge_with_cdprev(True)
    client = _mpd_client_with_status(elapsed=1.5)
    await bridge._previous_cdaware(client)
    client.previous.assert_awaited_once()
    client.seekid.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_cdaware_past_3s_seeks_to_start() -> None:
    bridge = _bridge_with_cdprev(True)
    client = _mpd_client_with_status(elapsed=12.0, songid="42")
    await bridge._previous_cdaware(client)
    client.seekid.assert_awaited_once_with(42, 0)
    client.previous.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_cdaware_at_3s_seeks_to_start() -> None:
    # Boundary: the original used ``>= 3``.
    bridge = _bridge_with_cdprev(True)
    client = _mpd_client_with_status(elapsed=3.0, songid="9")
    await bridge._previous_cdaware(client)
    client.seekid.assert_awaited_once_with(9, 0)
    client.previous.assert_not_awaited()
