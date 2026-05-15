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

import argparse
import asyncio
import configparser
import contextlib
import logging
import os
import re
import shlex
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import mpd
from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from mpd.asyncio import MPDClient

from mpdris2 import mpd_client
from mpdris2.cover import (
    DEFAULT_COVER_CACHE_DIR,
    DEFAULT_COVER_REGEX,
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
from mpdris2.notify import (
    Notifier,
    NotifierConfig,
    NotifyTemplates,
    format_template,
)
from mpdris2.translate import DEFAULT_URL_HANDLERS, mpd_to_mpris

logger = logging.getLogger(__name__)

BUS_CONNECT_TIMEOUT = 10.0

# Subsystems we care about — others (e.g. ``database``, ``update``,
# ``sticker``) don't influence the MPRIS-exposed state.
WATCHED_SUBSYSTEMS = frozenset({"player", "mixer", "options", "playlist"})


# --- Configuration resolution helpers -------------------------------------

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

    port_raw = (
        args.port
        or cfg.get("Connection", "port", fallback=None)
        or os.environ.get("MPD_PORT")
        or 6600
    )
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        logger.warning("invalid MPD port %r; falling back to 6600", port_raw)
        port = 6600
    return host, port, password


def _resolve_music_dir(
    cfg: configparser.ConfigParser,
    args: argparse.Namespace,
    *,
    socket: bool = False,
) -> Path | None:
    """Pick the music library path from CLI / config / XDG. Accepts a
    bare path or a ``file://`` URI; must resolve to an absolute local
    path — non-local URI schemes and relative paths are rejected
    (cover lookup needs local FS access, and ``Path.as_uri()``
    requires absolute).

    When ``socket=True`` the XDG fallback is skipped — MPD's ``config``
    command will hand us ``music_directory`` on first connect, which
    is more authoritative than guessing from XDG."""
    raw: str | None = (
        args.music_dir
        or cfg.get("Library", "music_dir", fallback=None)
        or cfg.get("Connection", "music_dir", fallback=None)
    )
    if not raw:
        return None if socket else _find_xdg_music_dir()
    path = Path(raw.removeprefix("file://")).expanduser()
    if not path.is_absolute():
        logger.warning(
            "music_dir %r must be a local absolute path; ignoring", raw,
        )
        return None
    return path


def _find_xdg_music_dir() -> Path | None:
    if "XDG_MUSIC_DIR" in os.environ:
        return Path(os.environ["XDG_MUSIC_DIR"])
    user_dirs = (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "user-dirs.dirs"
    )
    try:
        for line in user_dirs.read_text().splitlines():
            if not line.startswith("XDG_MUSIC_DIR="):
                continue
            path = shlex.split(line.removeprefix("XDG_MUSIC_DIR="))[0]
            if path.startswith("$HOME/"):
                return Path.home() / path.removeprefix("$HOME/")
            if path.startswith("/"):
                return Path(path)
    except OSError:
        pass
    for fallback in (Path.home() / "Music",
                     Path.home() / "Musique",
                     Path.home() / "music"):
        if fallback.is_dir():
            return fallback
    return None


def _resolve_cover_regex(cfg: configparser.ConfigParser) -> re.Pattern[str]:
    raw = cfg.get("Library", "cover_regex", fallback=None)
    if not raw:
        return DEFAULT_COVER_REGEX
    try:
        return re.compile(raw, re.I | re.X)
    except re.error as e:
        logger.warning("invalid cover_regex %r: %s; using default", raw, e)
        return DEFAULT_COVER_REGEX


def _resolve_cover_cache_dir(cfg: configparser.ConfigParser) -> Path:
    raw = cfg.get("Library", "cover_cache_dir", fallback=None)
    return Path(raw).expanduser() if raw else DEFAULT_COVER_CACHE_DIR


def _resolve_notify(cfg: configparser.ConfigParser) -> bool:
    # [Notify] preferred, fall back to deprecated [Bling].
    return cfg.getboolean(
        "Notify", "notify",
        fallback=cfg.getboolean("Bling", "notification", fallback=True),
    )


def _resolve_cdprev(cfg: configparser.ConfigParser) -> bool:
    return cfg.getboolean("Bling", "cdprev", fallback=False)


def _resolve_notify_paused(cfg: configparser.ConfigParser) -> bool:
    return cfg.getboolean("Bling", "notify_paused", fallback=False)


def _resolve_notify_templates(cfg: configparser.ConfigParser) -> NotifyTemplates:
    # raw=True so configparser doesn't try to interpolate the literal
    # ``%title%`` etc. as variables.
    return NotifyTemplates(
        summary=cfg.get("Notify", "summary", fallback="", raw=True),
        body=cfg.get("Notify", "body", fallback="", raw=True),
        paused_summary=cfg.get("Notify", "paused_summary", fallback="", raw=True),
        paused_body=cfg.get("Notify", "paused_body", fallback="", raw=True),
    )


def _resolve_notifier_config(cfg: configparser.ConfigParser) -> NotifierConfig:
    return NotifierConfig(
        urgency=cfg.getint("Notify", "urgency", fallback=1),
        timeout=cfg.getint("Notify", "timeout", fallback=-1),
    )


# --- Pure transformation helpers ------------------------------------------

def _loop_status_from(repeat: bool, single: bool) -> str:
    if repeat and single:
        return "Track"
    if repeat:
        return "Playlist"
    return "None"


def _playback_status_from(state: str) -> str:
    return {"play": "Playing", "pause": "Paused", "stop": "Stopped"}.get(state, "Stopped")


def _parse_volume(status: dict) -> float | None:
    """Return MPRIS-style volume (0.0..1.0) from MPD status, or None
    when MPD reports -1 (audio backend can't report — leave as-is)."""
    try:
        v = int(status.get("volume", -1))
    except (TypeError, ValueError):
        return None
    return v / 100.0 if v >= 0 else None


def _parse_elapsed(status: dict) -> float:
    try:
        return float(status.get("elapsed", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _is_external_seek(
    old_status: dict, old_time: float, new_pos_s: float, now: float
) -> bool:
    """Return True when the elapsed time deviates from what linear
    playback since ``old_time`` would predict by more than 0.6s — the
    same heuristic the original mpDris2 used to flag MPRIS-external
    seeks. Caller is responsible for checking that both sides are in
    the ``play`` state."""
    expected = float(old_status.get("elapsed", 0.0)) + (now - old_time)
    return abs(new_pos_s - expected) > 0.6


def _resolve_song_url(
    song: dict, music_dir: Path | None, url_handlers: list[str]
) -> str:
    file_uri = song.get("file", "") if song else ""
    if not file_uri:
        return ""
    if any(file_uri.startswith(h) for h in url_handlers) or not music_dir:
        return file_uri
    return (music_dir / file_uri).as_uri()


def _icon_path_for(meta: dict) -> str:
    """Libnotify wants a filesystem path for the icon, not a file:// URL."""
    value = getattr(meta.get("mpris:artUrl"), "value", "")
    return value.removeprefix("file://")


PAUSED_ICON = "media-playback-pause-symbolic"


def _build_track_notification(
    meta: dict, state: str = "play", position_us: int = 0,
    templates: NotifyTemplates | None = None,
) -> tuple[str, str, str]:
    """Compose (summary, body, icon). When the matching template is
    blank, fall back to the built-in default; ``paused_*`` falls back
    to ``summary`` / ``body`` before the built-in default."""
    templates = templates or NotifyTemplates()
    paused = state == "pause"
    summary_tpl = (templates.paused_summary if paused else "") or templates.summary
    body_tpl = (templates.paused_body if paused else "") or templates.body

    if summary_tpl:
        title = format_template(summary_tpl, meta, position_us=position_us)
    else:
        title_v = meta.get("xesam:title")
        title = str(getattr(title_v, "value", title_v) if title_v else "Unknown title")

    if body_tpl:
        body = format_template(body_tpl, meta, position_us=position_us)
    else:
        artists_v = meta.get("xesam:artist")
        artists = getattr(artists_v, "value", artists_v) if artists_v else ["Unknown artist"]
        body = f"by {', '.join(artists or ['Unknown artist'])}"
        if paused:
            body += " (Paused)"

    icon = PAUSED_ICON if paused else _icon_path_for(meta)
    return title, body, icon


# --- The bridge -----------------------------------------------------------

class MpdMprisBridge:
    """Single-event-loop bridge between MPD and MPRIS2."""

    def __init__(
        self, cfg: configparser.ConfigParser, args: argparse.Namespace,
    ) -> None:
        self._cfg = cfg
        self._args = args
        self._loop = asyncio.get_running_loop()

        # Per-connection state — rebound on each MPD reconnect.
        self.client: MPDClient | None = None
        self.caps: dict[str, bool] = {}
        self.last_status: dict = {}
        self.last_song: dict = {}
        self.last_time: float = 0.0

        # Resolved configuration.
        self.host, self.port, self.password = _resolve_endpoint(cfg, args)
        self.is_socket = self.host.startswith(("/", "@"))
        self.music_dir = _resolve_music_dir(cfg, args, socket=self.is_socket)
        if self.music_dir:
            logger.info("music library: %s", self.music_dir)

        self.url_handlers: list[str] = list(DEFAULT_URL_HANDLERS)

        # Strong-ref fire-and-forget tasks so the loop's weak refs don't
        # let them be GC'd mid-execution (asyncio docs explicitly warn).
        self.bg_tasks: set[asyncio.Task] = set()

        self.cover_finder = CoverFinder(CoverFinderConfig(
            music_dir=self.music_dir,
            cover_regex=_resolve_cover_regex(cfg),
            cover_cache_dir=_resolve_cover_cache_dir(cfg),
        ))
        self.notifier: Notifier | None = None  # set in setup() after bus
        self._cdprev = _resolve_cdprev(cfg)
        self._notify_paused = _resolve_notify_paused(cfg)
        self._notify_templates = _resolve_notify_templates(cfg)
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

    def on_play(self) -> None:     self._fire(lambda c: c.play())
    def on_pause(self) -> None:    self._fire(lambda c: c.pause(1))
    def on_stop(self) -> None:     self._fire(lambda c: c.stop())
    def on_next(self) -> None:     self._fire(lambda c: c.next())
    def on_previous(self) -> None: self._fire(self._previous_cdaware)

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
    def on_shuffle_set(self, v: bool) -> None: self._fire(lambda c: c.random(1 if v else 0))
    def on_volume_set(self, v: float) -> None: self._fire(lambda c: c.setvol(int(round(v * 100))))

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

    async def _build_track_metadata(
        self, song: dict, status: dict,
    ) -> dict[str, Variant]:
        """Translate ``song`` into MPRIS Metadata and resolve cover art.
        Cover lookup failures are swallowed (logged) — the metadata is
        still returned, just without ``mpris:artUrl``."""
        meta = mpd_to_mpris(song, self.music_dir, self.url_handlers)
        song_url = _resolve_song_url(song, self.music_dir, self.url_handlers)
        if not song_url:
            return meta
        try:
            cover = await self.cover_finder.find(SongLookup(
                client=self.client,
                song_uri=song_url,
                song_file=song.get("file", ""),
                mpd_meta=song,
                last_loaded_playlist=status.get("lastloadedplaylist", ""),
            ))
        except Exception:
            logger.exception("cover lookup failed")
            return meta
        if cover:
            meta["mpris:artUrl"] = Variant("s", cover)
        return meta

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

        now = self._loop.time()
        old_status, old_song, old_time = (
            self.last_status, self.last_song, self.last_time,
        )
        self.last_status, self.last_song, self.last_time = status, song, now

        state = status.get("state", "stop")
        self.player.update_playback_status(_playback_status_from(state))

        repeat = status.get("repeat", "0") == "1"
        single = status.get("single", "0") == "1"
        self.player.update_loop_status(_loop_status_from(repeat, single))
        self.player.update_shuffle(status.get("random", "0") == "1")

        vol = _parse_volume(status)
        if vol is not None:
            self.player.update_volume(vol)

        new_pos_s = _parse_elapsed(status)
        self.player.update_position(int(new_pos_s * 1_000_000))

        same_song = bool(
            old_song and song and old_song.get("id") == song.get("id")
        )
        if (
            same_song
            and old_status.get("state") == "play"
            and state == "play"
            and _is_external_seek(old_status, old_time, new_pos_s, now)
        ):
            self.player.emit_seeked(int(new_pos_s * 1_000_000))

        # CanGoNext: a next song is queued, or we'd loop back to the
        # start of the playlist anyway.
        self.player.update_capabilities(
            can_go_next="nextsongid" in status or repeat,
        )

        if not song:
            self.player.update_metadata({})
            self.player.update_capabilities(can_seek=False)
            return

        meta = await self._build_track_metadata(song, status)
        self.player.update_metadata(meta)
        self.player.update_capabilities(can_seek="mpris:length" in meta)

        # State-transition bubble: fire "Stopped" when playback
        # transitions from play/pause into stop. Track-change
        # notifications below handle the play / pause cases.
        if (self.notifier
                and old_status.get("state") in ("play", "pause")
                and state == "stop"):
            self._schedule(self.notifier.notify(
                IDENTITY, "Stopped", "media-playback-stop-symbolic",
            ))

        # Track-change notification — always when playing; also while
        # paused when ``[Bling] notify_paused`` is enabled.
        notify_state = state == "play" or (state == "pause" and self._notify_paused)
        if self.notifier and not same_song and notify_state:
            title, body, icon = _build_track_notification(
                meta, state, int(new_pos_s * 1_000_000), self._notify_templates,
            )
            self._schedule(self.notifier.notify(title, body, icon))

    # --- Lifecycle ------------------------------------------------------

    async def setup(self) -> None:
        """Acquire the session bus, export MPRIS interfaces, request the
        well-known name, and wire the notifier. Raises on timeout."""
        try:
            async with asyncio.timeout(BUS_CONNECT_TIMEOUT):
                self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
                self.bus.export(ROOT_PATH, MediaPlayer2())
                self.bus.export(ROOT_PATH, self.player)
                await self.bus.request_name(BUS_NAME)
        except TimeoutError:
            logger.critical(
                "D-Bus session bus did not respond within %.0fs; aborting",
                BUS_CONNECT_TIMEOUT,
            )
            raise
        logger.info("D-Bus name acquired: %s", BUS_NAME)
        if _resolve_notify(self._cfg):
            self.notifier = Notifier(
                self.bus, app_name="mpDris2",
                config=_resolve_notifier_config(self._cfg),
            )

    async def run_loop(self) -> None:
        """Outer MPD connect / reconnect loop. Returns when
        ``--no-reconnect`` is set or the initial connection is refused;
        raises ``CancelledError`` on shutdown signal."""
        while True:
            try:
                new_client = await mpd_client.connect(
                    self.host, self.port, self.password,
                    retry=not self._args.no_reconnect,
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
            if self._was_connected and self.notifier:
                self._schedule(self.notifier.notify(IDENTITY, "Reconnected", ""))
            self._was_connected = True
            self.caps = mpd_client.capabilities(cmds)
            logger.info("MPD capabilities: %s",
                        ",".join(k for k, v in self.caps.items() if v))
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

            try:
                self.url_handlers = list(await new_client.urlhandlers())
            except (mpd.CommandError, mpd.ConnectionError, OSError):
                self.url_handlers = list(DEFAULT_URL_HANDLERS)

            await self.refresh()

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
                    self._schedule(self.notifier.notify(
                        IDENTITY, "Disconnected", "error",
                    ))
            finally:
                with contextlib.suppress(Exception):
                    new_client.disconnect()
                self.client = None

            if self._args.no_reconnect:
                return
            # Reset MPRIS state so subscribers see "nothing playing"
            # while we reconnect.
            self.player.update_playback_status("Stopped")
            self.player.update_metadata({})

    async def close(self) -> None:
        """Drain in-flight tasks, release the bus name, disconnect."""
        logger.info("shutting down")
        for t in self.bg_tasks:
            t.cancel()
        if self.bg_tasks:
            await asyncio.gather(*self.bg_tasks, return_exceptions=True)
        self.cover_finder._discard_temp()
        bus = getattr(self, "bus", None)
        if bus is not None:
            with contextlib.suppress(Exception):
                await bus.release_name(BUS_NAME)
            with contextlib.suppress(Exception):
                bus.disconnect()
