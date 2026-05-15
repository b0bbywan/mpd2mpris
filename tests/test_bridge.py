"""Unit tests for bridge.py pure helpers + the ``_build_track_metadata``
method — no MPD, no D-Bus.

``_build_track_metadata`` runs on a partially-initialised
``MpdMprisBridge`` built via ``__new__`` (we skip the heavy ``__init__``
which needs a running event loop and resolves cfg/args). Only the
attributes the method reads are set on it.
"""

from __future__ import annotations

import argparse
import configparser
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dbus_fast import Variant

from mpdris2.bridge import (
    MpdMprisBridge,
    _build_track_notification,
    _find_xdg_music_dir,
    _icon_path_for,
    _is_external_seek,
    _loop_status_from,
    _parse_elapsed,
    _parse_volume,
    _playback_status_from,
    _resolve_cdprev,
    _resolve_music_dir,
    _resolve_notifier_config,
    _resolve_notify_paused,
    _resolve_notify_templates,
    _resolve_song_url,
)
from mpdris2.notify import NotifyTemplates


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


# --- _icon_path_for --------------------------------------------------------

def test_icon_path_for_file_uri_strips_scheme() -> None:
    meta = {"mpris:artUrl": Variant("s", "file:///tmp/cover.jpg")}
    assert _icon_path_for(meta) == "/tmp/cover.jpg"


def test_icon_path_for_plain_path_unchanged() -> None:
    meta = {"mpris:artUrl": Variant("s", "/tmp/cover.jpg")}
    assert _icon_path_for(meta) == "/tmp/cover.jpg"


def test_icon_path_for_missing() -> None:
    assert _icon_path_for({}) == ""


# --- _build_track_notification --------------------------------------------

def test_track_notification_full_meta() -> None:
    meta = {
        "xesam:title": Variant("s", "Song"),
        "xesam:artist": Variant("as", ["Artist A", "Artist B"]),
        "mpris:artUrl": Variant("s", "file:///tmp/c.jpg"),
    }
    title, body, icon = _build_track_notification(meta)
    assert title == "Song"
    assert body == "by Artist A, Artist B"
    assert icon == "/tmp/c.jpg"


def test_track_notification_missing_fields() -> None:
    title, body, icon = _build_track_notification({})
    assert title == "Unknown title"
    assert body == "by Unknown artist"
    assert icon == ""


def test_track_notification_paused_default_appends_marker() -> None:
    meta = {"xesam:title": Variant("s", "Song"),
            "xesam:artist": Variant("as", ["A"])}
    title, body, icon = _build_track_notification(meta, state="pause")
    assert title == "Song"
    assert body == "by A (Paused)"
    assert icon == "media-playback-pause-symbolic"


def test_track_notification_summary_template_expands() -> None:
    meta = {
        "xesam:title": Variant("s", "Song"),
        "xesam:album": Variant("s", "Album"),
        "xesam:artist": Variant("as", ["A"]),
    }
    templates = NotifyTemplates(summary="%artist% — %title%", body="from %album%")
    title, body, _icon = _build_track_notification(meta, templates=templates)
    assert title == "A — Song"
    assert body == "from Album"


def test_track_notification_paused_template_falls_back_to_playing() -> None:
    # No paused_summary → uses the playing template; no paused_body → same.
    meta = {"xesam:title": Variant("s", "Song"),
            "xesam:artist": Variant("as", ["A"])}
    templates = NotifyTemplates(summary="P:%title%", body="B:%artist%")
    title, body, icon = _build_track_notification(meta, state="pause", templates=templates)
    assert title == "P:Song"
    assert body == "B:A"
    assert icon == "media-playback-pause-symbolic"


def test_track_notification_paused_uses_paused_templates_when_set() -> None:
    meta = {"xesam:title": Variant("s", "Song"),
            "xesam:artist": Variant("as", ["A"])}
    templates = NotifyTemplates(
        summary="P:%title%", body="B:%artist%",
        paused_summary="zzz", paused_body="snoring",
    )
    title, body, _icon = _build_track_notification(meta, state="pause", templates=templates)
    assert title == "zzz"
    assert body == "snoring"


# --- _resolve_notify_templates --------------------------------------------

def test_resolve_notify_templates_defaults_blank() -> None:
    t = _resolve_notify_templates(configparser.ConfigParser())
    assert t == NotifyTemplates()


def test_resolve_notify_templates_explicit() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string(
        "[Notify]\n"
        "summary = %title%\n"
        "body = by %artist%\n"
        "paused_summary = (paused) %title%\n"
        "paused_body = was %artist%\n"
    )
    t = _resolve_notify_templates(cfg)
    assert t.summary == "%title%"
    assert t.body == "by %artist%"
    assert t.paused_summary == "(paused) %title%"
    assert t.paused_body == "was %artist%"


# --- _resolve_music_dir ----------------------------------------------------

def _ns(**overrides) -> argparse.Namespace:
    base = {"music_dir": None, "host": None, "port": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_resolve_music_dir_from_cli(tmp_path: Path) -> None:
    args = _ns(music_dir=str(tmp_path))
    cfg = configparser.ConfigParser()
    assert _resolve_music_dir(cfg, args) == tmp_path


def test_resolve_music_dir_from_file_uri_in_config() -> None:
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = file:///srv/music\n")
    assert _resolve_music_dir(cfg, args) == Path("/srv/music")


def test_resolve_music_dir_expands_tilde() -> None:
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = ~/Music\n")
    result = _resolve_music_dir(cfg, args)
    assert result == Path.home() / "Music"


def test_resolve_music_dir_non_local_scheme_returns_none(caplog) -> None:
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = http://example.com/music\n")
    with caplog.at_level("WARNING"):
        assert _resolve_music_dir(cfg, args) is None
    assert any("absolute" in r.message for r in caplog.records)


def test_resolve_music_dir_relative_path_returns_none(caplog) -> None:
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = Music\n")
    with caplog.at_level("WARNING"):
        assert _resolve_music_dir(cfg, args) is None
    assert any("absolute" in r.message for r in caplog.records)


def test_resolve_music_dir_file_uri_with_relative_path_rejected(caplog) -> None:
    """``file://relative`` is invalid per RFC 8089 and would crash later
    in ``Path.as_uri()`` — reject up front."""
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = file://Music\n")
    with caplog.at_level("WARNING"):
        assert _resolve_music_dir(cfg, args) is None


def test_resolve_music_dir_xdg_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_MUSIC_DIR", str(tmp_path))
    args = _ns()
    cfg = configparser.ConfigParser()
    assert _resolve_music_dir(cfg, args) == tmp_path


def test_resolve_music_dir_socket_skips_xdg_fallback(
    monkeypatch, tmp_path: Path,
) -> None:
    """On socket connections the XDG fallback is skipped — MPD's
    ``config`` command will give us the music_directory authoritatively."""
    monkeypatch.setenv("XDG_MUSIC_DIR", str(tmp_path))
    args = _ns()
    cfg = configparser.ConfigParser()
    assert _resolve_music_dir(cfg, args, socket=True) is None


def test_resolve_music_dir_socket_still_uses_explicit_config(
    tmp_path: Path,
) -> None:
    """Explicit config wins over socket auto-detect: user opted in."""
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string(f"[Library]\nmusic_dir = {tmp_path}\n")
    assert _resolve_music_dir(cfg, args, socket=True) == tmp_path


# --- _find_xdg_music_dir ---------------------------------------------------

def test_find_xdg_music_dir_env_var(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_MUSIC_DIR", str(tmp_path))
    assert _find_xdg_music_dir() == tmp_path


def test_find_xdg_music_dir_user_dirs_dollar_home(
    monkeypatch, tmp_path: Path,
) -> None:
    cfg_home = tmp_path / "config"
    cfg_home.mkdir()
    (cfg_home / "user-dirs.dirs").write_text('XDG_MUSIC_DIR="$HOME/MyMusic"\n')
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() resolves HOME at call time → fakehome.
    assert _find_xdg_music_dir() == tmp_path / "MyMusic"


def test_find_xdg_music_dir_user_dirs_absolute(
    monkeypatch, tmp_path: Path,
) -> None:
    cfg_home = tmp_path / "config"
    cfg_home.mkdir()
    (cfg_home / "user-dirs.dirs").write_text(
        f'XDG_MUSIC_DIR="{tmp_path}/abs_music"\n'
    )
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    assert _find_xdg_music_dir() == tmp_path / "abs_music"


def test_find_xdg_music_dir_directory_fallback(
    monkeypatch, tmp_path: Path,
) -> None:
    # No env vars, no user-dirs file — fall back to ~/Music if it exists.
    (tmp_path / "Music").mkdir()
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _find_xdg_music_dir() == tmp_path / "Music"


def test_find_xdg_music_dir_no_match(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    assert _find_xdg_music_dir() is None


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


# --- _resolve_cdprev -------------------------------------------------------

def test_resolve_cdprev_default_false() -> None:
    assert _resolve_cdprev(configparser.ConfigParser()) is False


def test_resolve_cdprev_explicit_true() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Bling]\ncdprev = True\n")
    assert _resolve_cdprev(cfg) is True


# --- _resolve_notifier_config ---------------------------------------------

def test_resolve_notifier_config_defaults() -> None:
    nc = _resolve_notifier_config(configparser.ConfigParser())
    assert nc.urgency == 1
    assert nc.timeout == -1


def test_resolve_notifier_config_explicit() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Notify]\nurgency = 2\ntimeout = 5000\n")
    nc = _resolve_notifier_config(cfg)
    assert nc.urgency == 2
    assert nc.timeout == 5000


# --- _resolve_notify_paused -----------------------------------------------

def test_resolve_notify_paused_default_false() -> None:
    assert _resolve_notify_paused(configparser.ConfigParser()) is False


def test_resolve_notify_paused_explicit_true() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Bling]\nnotify_paused = True\n")
    assert _resolve_notify_paused(cfg) is True


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
