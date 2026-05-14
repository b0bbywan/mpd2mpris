"""Daemon orchestration — asyncio runtime that wires MPD and D-Bus.

Single asyncio event loop. No threads, no GLib. MPRIS callbacks
schedule MPD commands as fire-and-forget tasks; MPD ``idle`` events
drive ``refresh()``, which translates the new status into MPRIS
properties and emits PropertiesChanged via ``MediaPlayer2Player``.

PR 2 ships the playback-state + transport-control surface (no
Metadata / cover / notify yet — those come in PR 3).
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Coroutine
from typing import Any

import mpd
from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from mpd.asyncio import MPDClient

from mpdris2 import mpd_client
from mpdris2.mpris import (
    BUS_NAME,
    ROOT_PATH,
    MediaPlayer2,
    MediaPlayer2Player,
)

logger = logging.getLogger(__name__)

# Subsystems we care about — others (e.g. ``database``, ``update``,
# ``sticker``) don't influence the MPRIS-exposed state.
WATCHED_SUBSYSTEMS = frozenset({"player", "mixer", "options", "playlist"})


def _resolve_endpoint(
    cfg: configparser.ConfigParser, args: argparse.Namespace
) -> tuple[str, int, str | None]:
    """Pick (host, port, password) from CLI args → config → env → defaults."""
    host = (
        args.host
        or cfg.get("Connection", "host", fallback=None)
        or os.environ.get("MPD_HOST")
        or "localhost"
    )
    password: str | None = cfg.get("Connection", "password", fallback=None) or None
    if "@" in host:
        # ``password@host`` shorthand matches the original mpDris2.
        password, host = host.rsplit("@", 1)

    port_str = (
        str(args.port) if args.port else cfg.get("Connection", "port", fallback="")
    ) or os.environ.get("MPD_PORT") or "6600"
    try:
        port = int(port_str)
    except ValueError:
        logger.warning("invalid MPD port %r; falling back to 6600", port_str)
        port = 6600
    return host, port, password


def _loop_status_from(repeat: bool, single: bool) -> str:
    if repeat and single:
        return "Track"
    if repeat:
        return "Playlist"
    return "None"


async def run(cfg: configparser.ConfigParser, args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    host, port, password = _resolve_endpoint(cfg, args)

    stop_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    # Strong-ref fire-and-forget tasks so the loop's weak refs don't
    # let them be GC'd mid-execution (asyncio docs explicitly warn).
    bg_tasks: set[asyncio.Task] = set()

    def schedule(coro: Coroutine[Any, Any, Any]) -> None:
        task = loop.create_task(coro)
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    async def mpd_safe(awaitable: Awaitable) -> Any:
        """Run an MPD coroutine; swallow command-level errors that don't
        matter for the MPRIS surface (no current song, invalid arg, …)
        and log connection drops without raising into the caller."""
        try:
            return await awaitable
        except mpd.CommandError as e:
            logger.debug("MPD command error: %s", e)
        except (mpd.ConnectionError, OSError) as e:
            logger.warning("MPD lost during command: %s", e)
        return None

    # --- State holders rebound on each reconnect -----------------
    client: MPDClient | None = None
    caps: dict[str, bool] = {}
    last_status: dict = {}
    last_song: dict = {}
    last_time: float = 0.0

    # --- MPRIS surface ------------------------------------------------
    def on_play() -> None:
        c = client
        if c is not None:
            schedule(mpd_safe(c.play()))

    def on_pause() -> None:
        c = client
        if c is not None:
            schedule(mpd_safe(c.pause(1)))

    def on_play_pause() -> None:
        c = client
        if c is None:
            return

        async def toggle() -> None:
            s = await mpd_safe(c.status())
            if s and s.get("state") == "play":
                await mpd_safe(c.pause(1))
            else:
                await mpd_safe(c.play())

        schedule(toggle())

    def on_stop() -> None:
        c = client
        if c is not None:
            schedule(mpd_safe(c.stop()))

    def on_next() -> None:
        c = client
        if c is not None:
            schedule(mpd_safe(c.next()))

    def on_previous() -> None:
        c = client
        if c is not None:
            schedule(mpd_safe(c.previous()))

    def on_seek(offset_us: int) -> None:
        c = client
        if c is None:
            return
        offset_s = offset_us / 1_000_000
        # MPD's seekcur accepts a string with a leading sign for relative
        # seeks; bare numbers are absolute.
        arg = f"+{offset_s}" if offset_us >= 0 else str(offset_s)
        schedule(mpd_safe(c.seekcur(arg)))

    def on_set_position(trackid: str, position_us: int) -> None:
        c = client
        if c is None:
            return
        # MPRIS requires the trackid match the currently playing track;
        # if it doesn't, the call is a no-op per spec.
        cur_id = last_song.get("id")
        if cur_id is not None and trackid != f"/org/mpris/MediaPlayer2/Track/{cur_id}":
            return
        position_s = position_us / 1_000_000
        schedule(mpd_safe(c.seekcur(str(position_s))))

    def on_volume_set(v: float) -> None:
        c = client
        if c is not None:
            schedule(mpd_safe(c.setvol(int(round(v * 100)))))

    def on_loop_status_set(val: str) -> None:
        c = client
        if c is None:
            return
        single_supported = caps.get("single", False)

        async def apply() -> None:
            if val == "Playlist":
                await mpd_safe(c.repeat(1))
                if single_supported:
                    await mpd_safe(c.single(0))
            elif val == "Track":
                await mpd_safe(c.repeat(1))
                if single_supported:
                    await mpd_safe(c.single(1))
            else:  # "None"
                await mpd_safe(c.repeat(0))
                if single_supported:
                    await mpd_safe(c.single(0))

        schedule(apply())

    def on_shuffle_set(v: bool) -> None:
        c = client
        if c is not None:
            schedule(mpd_safe(c.random(1 if v else 0)))

    root = MediaPlayer2()
    player = MediaPlayer2Player(
        on_play=on_play,
        on_pause=on_pause,
        on_play_pause=on_play_pause,
        on_stop=on_stop,
        on_next=on_next,
        on_previous=on_previous,
        on_seek=on_seek,
        on_set_position=on_set_position,
        on_volume_set=on_volume_set,
        on_loop_status_set=on_loop_status_set,
        on_shuffle_set=on_shuffle_set,
    )

    # --- D-Bus export (kept alive across MPD reconnects) -------------
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    bus.export(ROOT_PATH, root)
    bus.export(ROOT_PATH, player)
    await bus.request_name(BUS_NAME)
    logger.info("D-Bus name acquired: %s", BUS_NAME)

    # --- Refresh: MPD status -> MPRIS properties ---------------------
    async def refresh() -> None:
        nonlocal last_status, last_song, last_time
        c = client
        if c is None:
            return
        try:
            status = await c.status()
            song = await c.currentsong()
        except (mpd.ConnectionError, OSError) as e:
            logger.warning("MPD lost during refresh: %s", e)
            return

        now = loop.time()
        old_status = last_status
        old_song = last_song
        old_time = last_time
        last_status = status
        last_song = song
        last_time = now

        state = status.get("state", "stop")
        player.update_playback_status(
            {"play": "Playing", "pause": "Paused", "stop": "Stopped"}.get(state, "Stopped")
        )

        repeat = status.get("repeat", "0") == "1"
        single = status.get("single", "0") == "1"
        player.update_loop_status(_loop_status_from(repeat, single))
        player.update_shuffle(status.get("random", "0") == "1")

        # MPD: volume is 0-100, or -1 when the audio backend can't
        # report it (e.g. some ALSA configs). Treat -1 as "leave as-is".
        try:
            vol_raw = int(status.get("volume", -1))
        except (TypeError, ValueError):
            vol_raw = -1
        if vol_raw >= 0:
            player.update_volume(vol_raw / 100.0)

        # Position + Seeked detection. Same heuristic as the original
        # mpDris2: if the song didn't change and we were playing, the
        # elapsed time should advance linearly; a >0.6s deviation means
        # someone seeked outside of MPRIS.
        try:
            new_pos_s = float(status.get("elapsed", 0.0))
        except (TypeError, ValueError):
            new_pos_s = 0.0
        player.update_position(int(new_pos_s * 1_000_000))

        same_song = bool(
            old_song
            and song
            and old_song.get("id") == song.get("id")
        )
        if same_song and old_status.get("state") == "play" and state == "play":
            expected = float(old_status.get("elapsed", 0.0)) + (now - old_time)
            if abs(new_pos_s - expected) > 0.6:
                player.emit_seeked(int(new_pos_s * 1_000_000))

        # CanGoNext: a next song is queued, or we'd loop back to the
        # start of the playlist anyway.
        has_next = "nextsongid" in status or repeat
        player.update_capabilities(can_go_next=has_next)
        # CanSeek: until PR 3 wires real metadata, hard-True (matching
        # the original mpDris2). MPD will simply reject seekcur when
        # there's no current song.
        player.update_capabilities(can_seek=True)

    # --- Outer MPD connect / reconnect loop --------------------------
    try:
        while not stop_event.is_set():
            try:
                new_client = await mpd_client.connect(
                    host, port, password, retry=not args.no_reconnect
                )
            except (mpd.CommandError, mpd.ConnectionError, OSError) as e:
                logger.critical("MPD connection failed: %s", e)
                break

            client = new_client
            try:
                cmds = await new_client.commands()
            except (mpd.ConnectionError, OSError) as e:
                logger.warning("MPD dropped before commands probe: %s", e)
                client = None
                continue
            caps = mpd_client.capabilities(cmds)
            logger.info("MPD capabilities: %s",
                        ",".join(k for k, v in caps.items() if v))

            await refresh()

            try:
                async for subsystems in new_client.idle():
                    if stop_event.is_set():
                        break
                    if WATCHED_SUBSYSTEMS.intersection(subsystems):
                        await refresh()
            except (mpd.ConnectionError, OSError) as e:
                logger.warning("MPD idle loop ended: %s", e)
            finally:
                with contextlib.suppress(Exception):
                    new_client.disconnect()
                client = None

            if args.no_reconnect or stop_event.is_set():
                break
            # Reset MPRIS state so subscribers see "nothing playing"
            # while we reconnect.
            player.update_playback_status("Stopped")
            player.update_metadata({})
    finally:
        logger.info("shutting down")
        with contextlib.suppress(Exception):
            await bus.release_name(BUS_NAME)
        with contextlib.suppress(Exception):
            bus.disconnect()
