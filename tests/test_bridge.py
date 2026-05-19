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
    _RefreshSnapshot,
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


# --- _snapshot -------------------------------------------------------------

def _snapshot_bridge(
    *,
    last_status: dict | None = None,
    last_song: dict | None = None,
    last_time: float = 0.0,
    now: float = 100.0,
) -> MpdMprisBridge:
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge._loop = MagicMock()
    bridge._loop.time = MagicMock(return_value=now)
    bridge.last_status = last_status if last_status is not None else {}
    bridge.last_song = last_song if last_song is not None else {}
    bridge.last_time = last_time
    return bridge


def test_snapshot_captures_old_and_advances_last() -> None:
    bridge = _snapshot_bridge(
        last_status={"state": "play"},
        last_song={"id": "1"},
        last_time=42.0,
        now=100.0,
    )
    new_status = {"state": "pause", "elapsed": "12.5"}
    new_song = {"id": "2"}

    snap = bridge._snapshot(new_status, new_song)

    assert snap.old_status == {"state": "play"}
    assert snap.old_song == {"id": "1"}
    assert snap.old_time == 42.0
    assert snap.now == 100.0
    assert snap.state == "pause"
    assert snap.new_pos_s == 12.5
    assert snap.same_song is False
    # self.last_* advanced to the new values.
    assert bridge.last_status is new_status
    assert bridge.last_song is new_song
    assert bridge.last_time == 100.0


def test_snapshot_same_song_when_ids_match() -> None:
    bridge = _snapshot_bridge(last_song={"id": "7"})
    snap = bridge._snapshot({"state": "play"}, {"id": "7"})
    assert snap.same_song is True


def test_snapshot_first_refresh_is_not_same_song() -> None:
    # No previous song → same_song must be False so track-change
    # notifications fire on the very first track.
    bridge = _snapshot_bridge()
    snap = bridge._snapshot({"state": "play"}, {"id": "1"})
    assert snap.same_song is False


def test_snapshot_state_defaults_to_stop_when_missing() -> None:
    bridge = _snapshot_bridge()
    snap = bridge._snapshot({}, {})
    assert snap.state == "stop"
    assert snap.new_pos_s == 0.0


# --- _apply_current_state --------------------------------------------------

def _apply_bridge(cover_finder=None, **player_calls) -> MpdMprisBridge:
    """Bridge with a mocked player (capture update_* calls) and the
    minimal cover/music wiring ``_build_track_metadata`` needs."""
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge.client = MagicMock()
    bridge.music_dir = Path("/srv/music")
    bridge.url_handlers = ["http://"]
    if cover_finder is None:
        cover_finder = MagicMock()
        cover_finder.find = AsyncMock(return_value=None)
    bridge.cover_finder = cover_finder
    bridge.player = MagicMock()
    return bridge


def _snap(
    *,
    old_state: str = "stop", state: str = "play",
    old_time: float = 0.0, now: float = 10.0,
    old_elapsed: float = 0.0, new_pos_s: float = 0.0,
    same_song: bool = False, old_song: dict | None = None,
) -> _RefreshSnapshot:
    return _RefreshSnapshot(
        old_status={"state": old_state, "elapsed": str(old_elapsed)},
        old_song=old_song if old_song is not None else {},
        old_time=old_time,
        now=now,
        state=state,
        new_pos_s=new_pos_s,
        same_song=same_song,
    )


@pytest.mark.asyncio
async def test_apply_pushes_basic_player_state() -> None:
    bridge = _apply_bridge()
    status = {
        "state": "play", "elapsed": "5.0",
        "repeat": "1", "single": "1", "random": "1", "volume": "50",
    }
    await bridge._apply_current_state(
        status, {"id": "1", "title": "x"},
        _snap(state="play", new_pos_s=5.0),
    )
    bridge.player.update_playback_status.assert_called_with("Playing")
    bridge.player.update_loop_status.assert_called_with("Track")
    bridge.player.update_shuffle.assert_called_with(True)
    bridge.player.update_volume.assert_called_with(0.5)
    bridge.player.update_position.assert_called_with(5_000_000)


@pytest.mark.asyncio
async def test_apply_skips_volume_when_unreportable() -> None:
    bridge = _apply_bridge()
    await bridge._apply_current_state(
        {"state": "play", "volume": "-1"}, {"id": "1"}, _snap(),
    )
    bridge.player.update_volume.assert_not_called()


@pytest.mark.asyncio
async def test_apply_emits_seeked_on_external_seek() -> None:
    bridge = _apply_bridge()
    # 10s wall-clock elapsed since old_time=0, old elapsed=5 → expected 15s;
    # new_pos_s=30s → external seek.
    await bridge._apply_current_state(
        {"state": "play"}, {"id": "1"},
        _snap(old_state="play", state="play", same_song=True,
              old_elapsed=5.0, old_time=0.0, now=10.0, new_pos_s=30.0),
    )
    bridge.player.emit_seeked.assert_called_once_with(30_000_000)


@pytest.mark.asyncio
async def test_apply_no_seeked_on_natural_progression() -> None:
    bridge = _apply_bridge()
    await bridge._apply_current_state(
        {"state": "play"}, {"id": "1"},
        _snap(old_state="play", state="play", same_song=True,
              old_elapsed=5.0, old_time=0.0, now=10.0, new_pos_s=15.0),
    )
    bridge.player.emit_seeked.assert_not_called()


@pytest.mark.asyncio
async def test_apply_no_seeked_on_song_change() -> None:
    bridge = _apply_bridge()
    await bridge._apply_current_state(
        {"state": "play"}, {"id": "2"},
        _snap(old_state="play", state="play", same_song=False,
              new_pos_s=30.0),
    )
    bridge.player.emit_seeked.assert_not_called()


@pytest.mark.asyncio
async def test_apply_can_go_next_from_nextsongid() -> None:
    bridge = _apply_bridge()
    await bridge._apply_current_state(
        {"state": "play", "nextsongid": "5"}, {"id": "1"}, _snap(),
    )
    bridge.player.update_capabilities.assert_any_call(can_go_next=True)


@pytest.mark.asyncio
async def test_apply_can_go_next_from_repeat() -> None:
    bridge = _apply_bridge()
    await bridge._apply_current_state(
        {"state": "play", "repeat": "1"}, {"id": "1"}, _snap(),
    )
    bridge.player.update_capabilities.assert_any_call(can_go_next=True)


@pytest.mark.asyncio
async def test_apply_no_song_clears_metadata_and_returns_empty() -> None:
    bridge = _apply_bridge()
    meta = await bridge._apply_current_state(
        {"state": "stop"}, {}, _snap(state="stop"),
    )
    assert meta == {}
    bridge.player.update_metadata.assert_called_with({})
    bridge.player.update_capabilities.assert_any_call(can_seek=False)


@pytest.mark.asyncio
async def test_apply_song_returns_meta_with_can_seek() -> None:
    bridge = _apply_bridge()
    meta = await bridge._apply_current_state(
        {"state": "play"},
        {"id": "1", "title": "Track", "time": "180"},
        _snap(state="play"),
    )
    assert "xesam:title" in meta
    bridge.player.update_metadata.assert_called_with(meta)
    bridge.player.update_capabilities.assert_any_call(can_seek=True)


# --- _emit_notifications ---------------------------------------------------

def _notif_bridge(*, notifier=None, notify_paused: bool = False) -> MpdMprisBridge:
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge.notifier = notifier
    bridge._notify_paused = notify_paused
    bridge._schedule = MagicMock()  # type: ignore[method-assign]
    return bridge


def _fake_notifier():
    n = MagicMock()
    n.notify = MagicMock(return_value=MagicMock())
    n.notify_track = MagicMock(return_value=MagicMock())
    return n


def test_emit_no_notifier_is_noop() -> None:
    bridge = _notif_bridge(notifier=None)
    bridge._emit_notifications(_snap(state="stop", old_state="play"), {"x": 1})
    # Nothing to assert other than no crash; _schedule was replaced
    # with a mock and should not have been called.
    bridge._schedule.assert_not_called()  # type: ignore[attr-defined]


def test_emit_empty_meta_is_noop() -> None:
    """Empty queue (no current song) keeps the daemon silent — preserves
    the early-return behaviour of the pre-refactor ``refresh``."""
    notifier = _fake_notifier()
    bridge = _notif_bridge(notifier=notifier)
    bridge._emit_notifications(
        _snap(old_state="play", state="stop"), meta={},
    )
    bridge._schedule.assert_not_called()  # type: ignore[attr-defined]
    notifier.notify.assert_not_called()
    notifier.notify_track.assert_not_called()


def test_emit_stopped_bubble_on_play_to_stop() -> None:
    notifier = _fake_notifier()
    bridge = _notif_bridge(notifier=notifier)
    bridge._emit_notifications(
        _snap(old_state="play", state="stop", same_song=True), {"x": 1},
    )
    notifier.notify.assert_called_once()
    # Stopped is the one-shot — track-change must not also fire.
    notifier.notify_track.assert_not_called()


def test_emit_no_stopped_on_stop_to_stop() -> None:
    notifier = _fake_notifier()
    bridge = _notif_bridge(notifier=notifier)
    bridge._emit_notifications(
        _snap(old_state="stop", state="stop"), {"x": 1},
    )
    notifier.notify.assert_not_called()


def test_emit_track_change_on_play() -> None:
    notifier = _fake_notifier()
    bridge = _notif_bridge(notifier=notifier)
    bridge._emit_notifications(
        _snap(old_state="play", state="play",
              same_song=False, new_pos_s=2.5),
        {"xesam:title": "x"},
    )
    notifier.notify_track.assert_called_once()
    args, _kwargs = notifier.notify_track.call_args
    assert args[1] == "play"
    assert args[2] == 2_500_000


def test_emit_no_track_change_on_same_song() -> None:
    notifier = _fake_notifier()
    bridge = _notif_bridge(notifier=notifier)
    bridge._emit_notifications(
        _snap(old_state="play", state="play", same_song=True), {"x": 1},
    )
    notifier.notify_track.assert_not_called()


def test_emit_no_track_change_when_paused_without_flag() -> None:
    notifier = _fake_notifier()
    bridge = _notif_bridge(notifier=notifier, notify_paused=False)
    bridge._emit_notifications(
        _snap(old_state="play", state="pause", same_song=False), {"x": 1},
    )
    notifier.notify_track.assert_not_called()


def test_emit_track_change_when_paused_with_flag() -> None:
    notifier = _fake_notifier()
    bridge = _notif_bridge(notifier=notifier, notify_paused=True)
    bridge._emit_notifications(
        _snap(old_state="play", state="pause", same_song=False), {"x": 1},
    )
    notifier.notify_track.assert_called_once()
