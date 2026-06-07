"""MpdMprisBridge — single-event-loop bridge between MPD and MPRIS2.

The bridge owns the per-connection state (MPD client, capabilities,
last status snapshot) and the long-lived resources (D-Bus connection,
cover finder, notifier). MPRIS callbacks are methods rather than
closures so they're testable in isolation and don't need ``nonlocal``.

Shutdown is driven by ``CancelledError`` propagating from the
top-level task (``cli._amain`` installs the signal handlers). No
``stop_event`` flag — that pattern doesn't unblock the
``client.idle()`` await, which can hang on a quiet MPD.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from gettext import gettext as _
from pathlib import Path
from typing import Any

import mpd
from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from mpd.asyncio import MPDClient

from mpdris2 import mpd_client
from mpdris2.cover import (
    CoverFinder,
    CoverFinderConfig,
    SongLookup,
)
from mpdris2.mpris import (
    BUS_NAME,
    IDENTITY,
    ROOT_PATH,
    MediaPlayer2,
    MediaPlayer2Player,
)
from mpdris2.notify import Notifier
from mpdris2.translate import (
    DEFAULT_URL_HANDLERS,
    loop_status_from,
    mpd_to_mpris,
    parse_elapsed,
    parse_loop_flags,
    parse_shuffle,
    parse_volume,
    playback_status_from,
    song_url,
)

logger = logging.getLogger(__name__)

# Subsystems we care about — others (e.g. ``database``, ``update``,
# ``sticker``) don't influence the MPRIS-exposed state.
WATCHED_SUBSYSTEMS = frozenset({"player", "mixer", "options", "playlist"})


# --- Configuration resolution helpers -------------------------------------


@dataclass(frozen=True)
class BridgeConfig:
    """Pre-resolved runtime config for the bridge. cli.py builds it
    from configparser + argparse; the bridge itself never touches
    either."""

    host: str
    port: int
    password: str | None
    is_socket: bool
    music_dir: Path | None
    cover_regex: re.Pattern[str]
    cover_itunes: bool
    cover_deezer: bool
    cdprev: bool
    notify_paused: bool
    no_reconnect: bool


# --- Bridge-local helpers -------------------------------------------------
# Pure MPD→MPRIS shape conversions (``parse_volume``, ``loop_status_from``,
# ``song_url`` …) live in ``mpdris2.translate``. What stays here is either
# stateful (the refresh diff) or a heuristic that's specific to the bridge's
# refresh cadence (external-seek detection).


def _is_external_seek(old_status: dict, old_time: float, new_pos_s: float, now: float) -> bool:
    """Return True when the elapsed time deviates from what linear
    playback since ``old_time`` would predict by more than 0.6s — the
    same heuristic the original mpDris2 used to flag MPRIS-external
    seeks. Caller is responsible for checking that both sides are in
    the ``play`` state."""
    expected = float(old_status.get("elapsed", 0.0)) + (now - old_time)
    return abs(new_pos_s - expected) > 0.6


@dataclass(frozen=True)
class _RefreshSnapshot:
    """Per-refresh diff between the previous and current MPD status.
    Carries the old values (for transition detection) plus the few new
    values both ``_apply_current_state`` and ``_emit_notifications``
    consume — so neither helper has to re-derive them."""

    old_status: dict
    old_song: dict
    old_time: float
    now: float
    state: str
    new_pos_s: float
    same_song: bool


# --- The bridge -----------------------------------------------------------


class MpdMprisBridge:
    """Single-event-loop bridge between MPD and MPRIS2."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        bus: MessageBus,
        notifier: Notifier | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()

        # Per-connection state — rebound on each MPD reconnect.
        self.client: MPDClient | None = None
        self.caps: dict[str, bool] = {}
        self.last_status: dict = {}
        self.last_song: dict = {}
        self.last_time: float = 0.0

        # Pre-resolved configuration (built in cli.py).
        self.host = config.host
        self.port = config.port
        self.password = config.password
        self.is_socket = config.is_socket
        # ``music_dir`` is mutable on ``self`` because run_loop() may
        # learn it later from MPD's ``config`` command on a socket.
        self.music_dir = config.music_dir
        if self.music_dir:
            logger.info("music library: %s", self.music_dir)

        self.url_handlers: list[str] = list(DEFAULT_URL_HANDLERS)

        # Strong-ref fire-and-forget tasks so the loop's weak refs don't
        # let them be GC'd mid-execution (asyncio docs explicitly warn).
        self.bg_tasks: set[asyncio.Task] = set()

        # Last cover-free Metadata emitted, for change detection (a fresh
        # base != this means the track or web-radio ICY title changed);
        # the artUrl currently shown, carried into every re-emit so it
        # never flickers away (a web-radio stream churns its metadata while
        # the same cover/favicon still applies); plus the in-flight cover
        # lookup so a stale one can be cancelled.
        self._last_base: dict[str, Variant] = {}
        self._art: str | None = None
        self._cover_task: asyncio.Task | None = None

        self.cover_finder = CoverFinder(
            CoverFinderConfig(
                music_dir=self.music_dir,
                cover_regex=config.cover_regex,
                use_itunes=config.cover_itunes,
                use_deezer=config.cover_deezer,
            )
        )
        self.bus = bus
        self.notifier = notifier
        self._cdprev = config.cdprev
        self._notify_paused = config.notify_paused
        self._no_reconnect = config.no_reconnect
        # ``True`` once we've held a live MPD connection at least once
        # — gates the "Reconnected" / "Disconnected" bubbles so neither
        # fires on the very first connect attempt.
        self._was_connected = False

        self.player = MediaPlayer2Player(
            on_play=self.on_play,
            on_pause=self.on_pause,
            on_play_pause=self.on_play_pause,
            on_stop=self.on_stop,
            on_next=self.on_next,
            on_previous=self.on_previous,
            on_seek=self.on_seek,
            on_set_position=self.on_set_position,
            on_volume_set=self.on_volume_set,
            on_loop_status_set=self.on_loop_status_set,
            on_shuffle_set=self.on_shuffle_set,
            on_get_position=self.on_get_position,
        )

    # --- Task / error plumbing ------------------------------------------

    def _on_bg_done(self, task: asyncio.Task) -> None:
        self.bg_tasks.discard(task)
        if task.cancelled():
            return
        # Calling ``exception()`` marks the result as retrieved, so we
        # also lose the asyncio "Task exception was never retrieved"
        # warning — replace it with a logger error that carries the
        # full traceback and our log formatting.
        exc = task.exception()
        if exc is not None:
            logger.error("background task crashed: %r", exc, exc_info=exc)

    def _schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = self._loop.create_task(coro)
        self.bg_tasks.add(task)
        task.add_done_callback(self._on_bg_done)

    async def _mpd_safe(self, awaitable: Awaitable) -> Any:
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

    def _fire(self, mpd_call: Callable[[MPDClient], Awaitable]) -> None:
        """Schedule a one-shot MPD command from a sync MPRIS callback.
        No-op when there's no live connection (during reconnect)."""
        c = self.client
        if c is not None:
            self._schedule(self._mpd_safe(mpd_call(c)))

    # --- MPRIS callbacks ------------------------------------------------

    def on_play(self) -> None:
        self._fire(lambda c: c.play())

    def on_pause(self) -> None:
        self._fire(lambda c: c.pause(1))

    def on_stop(self) -> None:
        self._fire(lambda c: c.stop())

    def on_next(self) -> None:
        self._fire(lambda c: c.next())

    def on_previous(self) -> None:
        self._fire(self._previous_cdaware)

    def on_shuffle_set(self, v: bool) -> None:
        self._fire(lambda c: c.random(1 if v else 0))

    def on_volume_set(self, v: float) -> None:
        self._fire(lambda c: c.setvol(int(round(v * 100))))

    async def _previous_cdaware(self, c: MPDClient) -> None:
        """CD-like ``previous``: when ``cdprev`` is enabled and we're
        more than 3 s into the current track, seek to the start
        instead of skipping to the previous track."""
        if self._cdprev:
            status = await c.status()
            if float(status.get("elapsed", 0.0)) >= 3 and "songid" in status:
                await c.seekid(int(status["songid"]), 0)
                return
        await c.previous()

    def on_play_pause(self) -> None:
        c = self.client
        if c is None:
            return

        async def toggle() -> None:
            s = await self._mpd_safe(c.status())
            if s and s.get("state") == "play":
                await self._mpd_safe(c.pause(1))
            else:
                await self._mpd_safe(c.play())

        self._schedule(toggle())

    def on_seek(self, offset_us: int) -> None:
        # MPD's seekcur accepts a string with a leading sign for relative
        # seeks; bare numbers are absolute.
        offset_s = offset_us / 1_000_000
        arg = f"+{offset_s}" if offset_us >= 0 else str(offset_s)
        self._fire(lambda c: c.seekcur(arg))

    async def on_get_position(self) -> int | None:
        # Live read for the MPRIS Position property: query MPD's current
        # elapsed time. Returns None when there's no live connection, so the
        # interface falls back to its last cached value.
        c = self.client
        if c is None:
            return None
        status = await self._mpd_safe(c.status())
        if not status:
            return None
        return int(parse_elapsed(status) * 1_000_000)

    def on_set_position(self, trackid: str, position_us: int) -> None:
        # MPRIS requires the trackid match the currently playing track;
        # if it doesn't, the call is a no-op per spec.
        cur_id = self.last_song.get("id")
        if cur_id is not None and trackid != f"/org/mpris/MediaPlayer2/Track/{cur_id}":
            return
        position_s = position_us / 1_000_000
        self._fire(lambda c: c.seekcur(str(position_s)))

    def on_loop_status_set(self, val: str) -> None:
        c = self.client
        if c is None:
            return
        single_supported = self.caps.get("single", False)

        async def apply() -> None:
            if val == "Playlist":
                await self._mpd_safe(c.repeat(1))
                if single_supported:
                    await self._mpd_safe(c.single(0))
            elif val == "Track":
                await self._mpd_safe(c.repeat(1))
                if single_supported:
                    await self._mpd_safe(c.single(1))
            else:  # "None"
                await self._mpd_safe(c.repeat(0))
                if single_supported:
                    await self._mpd_safe(c.single(0))

        self._schedule(apply())

    # --- Metadata + cover -----------------------------------------------

    def _with_art(self, base: dict[str, Variant]) -> dict[str, Variant]:
        """``base`` plus the currently-shown ``mpris:artUrl`` (if any), so a
        re-emit keeps the cover instead of blanking it."""
        if self._art is None:
            return base
        return {**base, "mpris:artUrl": Variant("s", self._art)}

    def _cancel_cover(self) -> None:
        if self._cover_task and not self._cover_task.done():
            self._cover_task.cancel()
        self._cover_task = None

    def _schedule_cover(self, song: dict, status: dict, snap: _RefreshSnapshot, base: dict[str, Variant]) -> None:
        """Resolve the cover for ``base`` off the critical path, replacing
        any in-flight lookup for the previous track."""
        self._cancel_cover()
        task = self._loop.create_task(self._resolve_cover(song, status, snap, base))
        self._cover_task = task
        self.bg_tasks.add(task)
        task.add_done_callback(self._on_bg_done)

    async def _resolve_cover(
        self,
        song: dict,
        status: dict,
        snap: _RefreshSnapshot,
        base: dict[str, Variant],
    ) -> None:
        """Resolve cover art (the slow, network-bound part) then re-emit
        Metadata with ``mpris:artUrl`` and fire the track-change bubble —
        now carrying the cover. Bails when the track changed while we were
        resolving, so a slow lookup never lands on the wrong song."""
        cover = None
        url = song_url(song, self.music_dir, self.url_handlers)
        if url:
            try:
                cover = await self.cover_finder.find(
                    SongLookup(
                        client=self.client,
                        song_uri=url,
                        song_file=song.get("file", ""),
                        mpd_meta=song,
                        last_loaded_playlist=status.get("lastloadedplaylist", ""),
                    )
                )
            except Exception:
                logger.exception("cover lookup failed")
        if self._last_base is not base:  # track moved on while resolving
            return
        # Commit the result, re-emitting on change — blanks a stale cover
        # carried over when a web-radio title change resolves to none.
        if cover != self._art:
            self._art = cover
            self.player.update_metadata(self._with_art(base))
        self._maybe_notify_track(snap, self._with_art(base))

    # --- Refresh: MPD status -> MPRIS properties ------------------------

    async def refresh(self) -> None:
        c = self.client
        if c is None:
            return
        try:
            status = await c.status()
            song = await c.currentsong()
        except (mpd.ConnectionError, OSError) as e:
            logger.warning("MPD lost during refresh: %s", e)
            return

        snap = self._snapshot(status, song)
        self._apply_current_state(status, song, snap)
        self._maybe_notify_stop(snap, song)

    def _snapshot(self, status: dict, song: dict) -> _RefreshSnapshot:
        """Capture the previous status/song/time, advance ``self.last_*``
        to the new values, and return the deltas + derived values that
        both ``_apply_current_state`` and ``_emit_notifications`` need."""
        now = self._loop.time()
        snap = _RefreshSnapshot(
            old_status=self.last_status,
            old_song=self.last_song,
            old_time=self.last_time,
            now=now,
            state=status.get("state", "stop"),
            new_pos_s=parse_elapsed(status),
            same_song=bool(self.last_song and song and self.last_song.get("id") == song.get("id")),
        )
        self.last_status, self.last_song, self.last_time = status, song, now
        return snap

    def _apply_current_state(
        self,
        status: dict,
        song: dict,
        snap: _RefreshSnapshot,
    ) -> None:
        """Push the current MPD state onto the MPRIS player interface.
        Emits ``Seeked`` when an external seek is detected against the
        previous snapshot, and Metadata (cover-free) the moment the track
        changes — the cover is then resolved off the critical path."""
        self.player.update_playback_status(playback_status_from(snap.state))

        repeat, single = parse_loop_flags(status)
        self.player.update_loop_status(loop_status_from(repeat, single))
        self.player.update_shuffle(parse_shuffle(status))

        vol = parse_volume(status)
        if vol is not None:
            self.player.update_volume(vol)

        self.player.update_position(int(snap.new_pos_s * 1_000_000))

        if (
            snap.same_song
            and snap.old_status.get("state") == "play"
            and snap.state == "play"
            and _is_external_seek(
                snap.old_status,
                snap.old_time,
                snap.new_pos_s,
                snap.now,
            )
        ):
            self.player.emit_seeked(int(snap.new_pos_s * 1_000_000))

        # CanGoNext: a next song is queued, or we'd loop back to the
        # start of the playlist anyway.
        self.player.update_capabilities(
            can_go_next="nextsongid" in status or repeat,
        )

        if not song:
            self.player.update_metadata({})
            self.player.update_capabilities(can_seek=False)
            self._last_base = {}
            self._art = None
            self._cancel_cover()
            return

        # Emit Metadata immediately so clients update the instant the track
        # (or web-radio ICY title) changes; the cover is resolved off the
        # critical path. A status-only refresh (same tags) leaves the
        # already-emitted metadata untouched. The current artUrl is carried
        # into the new emit — a real track change drops it (a fresh cover is
        # coming), a same-stream title change keeps it so it never blanks.
        base = mpd_to_mpris(song, self.music_dir, self.url_handlers)
        if base == self._last_base:
            return
        self._last_base = base
        if not snap.same_song:
            self._art = None
        self.player.update_metadata(self._with_art(base))
        self.player.update_capabilities(can_seek="mpris:length" in base)
        self._schedule_cover(song, status, snap, base)

    def _maybe_notify_stop(self, snap: _RefreshSnapshot, song: dict) -> None:
        """One-shot "Stopped" bubble on a play/pause → stop transition.
        Requires a current song — an empty queue should stay silent."""
        if not self.notifier or not song:
            return
        if snap.old_status.get("state") in ("play", "pause") and snap.state == "stop":
            self._schedule(
                self.notifier.notify(
                    IDENTITY,
                    _("Stopped"),
                    "media-playback-stop-symbolic",
                )
            )

    def _maybe_notify_track(self, snap: _RefreshSnapshot, meta: dict[str, Variant]) -> None:
        """Track-change bubble while playing (or paused when ``[Bling]
        notify_paused`` is on). Fired from the cover path so the bubble
        carries the cover; gated on a real song change."""
        if not self.notifier or not meta:
            return
        notify_state = snap.state == "play" or (snap.state == "pause" and self._notify_paused)
        if not snap.same_song and notify_state:
            self._schedule(
                self.notifier.notify_track(
                    meta,
                    snap.state,
                    int(snap.new_pos_s * 1_000_000),
                )
            )

    # --- Lifecycle ------------------------------------------------------

    async def setup(self) -> None:
        """Export MPRIS interfaces on the injected bus and request the
        well-known name. The bus + notifier come pre-built from cli.py."""
        self.bus.export(ROOT_PATH, MediaPlayer2())
        self.bus.export(ROOT_PATH, self.player)
        await self.bus.request_name(BUS_NAME)
        logger.info("D-Bus name acquired: %s", BUS_NAME)

    async def run_loop(self) -> None:
        """Outer MPD connect / reconnect loop. Returns when
        ``--no-reconnect`` is set or the initial connection is refused;
        raises ``CancelledError`` on shutdown signal."""
        while True:
            try:
                new_client = await mpd_client.connect(
                    self.host,
                    self.port,
                    self.password,
                    retry=not self._no_reconnect,
                )
            except (mpd.CommandError, mpd.ConnectionError, OSError) as e:
                logger.critical("MPD connection failed: %s", e)
                return

            self.client = new_client
            try:
                cmds = await new_client.commands()
            except (mpd.ConnectionError, OSError) as e:
                logger.warning("MPD dropped before commands probe: %s", e)
                self.client = None
                continue
            self.caps = mpd_client.capabilities(cmds)
            logger.info("MPD capabilities: %s", ",".join(k for k, v in self.caps.items() if v))
            self.cover_finder.update_capabilities(
                can_readpicture=self.caps["readpicture"],
                can_albumart=self.caps["albumart"],
            )

            # On a Unix socket MPD allows the ``config`` command; use it
            # to auto-pick the music_directory when the user hasn't
            # configured one. TCP clients get "Access denied", so we
            # only attempt this over a socket.
            if self.music_dir is None and self.is_socket:
                try:
                    server_cfg = await mpd_client.fetch_config(new_client)
                    md = server_cfg.get("music_directory")
                except (mpd.CommandError, mpd.ConnectionError, OSError) as e:
                    logger.debug("MPD config lookup failed: %s", e)
                    md = None
                if md:
                    self.music_dir = Path(md)
                    self.cover_finder.update_music_dir(self.music_dir)
                    logger.info("music library (from MPD): %s", self.music_dir)

            if self.music_dir is None:
                logger.warning(
                    "no music_dir configured; xesam:url will be relative, breaking the MPRIS2 spec",
                )

            try:
                self.url_handlers = list(await new_client.urlhandlers())
            except (mpd.CommandError, mpd.ConnectionError, OSError):
                self.url_handlers = list(DEFAULT_URL_HANDLERS)

            await self.refresh()

            # Fire the reconnect bubble *after* refresh so MPRIS
            # subscribers see the fresh metadata before the popup.
            if self._was_connected and self.notifier:
                self._schedule(self.notifier.notify(IDENTITY, _("Reconnected"), ""))
            self._was_connected = True

            try:
                async for subsystems in new_client.idle():
                    if WATCHED_SUBSYSTEMS.intersection(subsystems):
                        await self.refresh()
            except (mpd.ConnectionError, OSError) as e:
                logger.warning("MPD idle loop ended: %s", e)
                # Genuine MPD drop, not an intentional shutdown — emit
                # the bubble before tearing down so it appears while
                # the bus is still healthy.
                if self.notifier:
                    self._schedule(
                        self.notifier.notify(
                            IDENTITY,
                            _("Disconnected"),
                            "error",
                        )
                    )
            finally:
                with contextlib.suppress(Exception):
                    new_client.disconnect()
                self.client = None

            if self._no_reconnect:
                return
            # Reset MPRIS state so subscribers see "nothing playing"
            # while we reconnect.
            self.player.update_playback_status("Stopped")
            self.player.update_metadata({})
            self._last_base = {}
            self._art = None
            self._cancel_cover()

    async def close(self) -> None:
        """Drain in-flight tasks and release the bus name. The bus
        itself is owned by cli.py and disconnected there."""
        logger.info("shutting down")
        for t in self.bg_tasks:
            t.cancel()
        if self.bg_tasks:
            await asyncio.gather(*self.bg_tasks, return_exceptions=True)
        self.cover_finder.close()
        with contextlib.suppress(Exception):
            await self.bus.release_name(BUS_NAME)
