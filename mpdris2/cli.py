"""CLI entry point: argparse + config loading + asyncio dispatch.

Kept separate from the daemon runtime (``mpdris2.daemon``) so the
bootstrap surface (argument parsing, config resolution) is testable in
isolation from the asyncio event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import contextlib
import gettext
import logging
import os
import re
import shlex
import signal
import sys
from pathlib import Path

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from mpdris2.bridge import BridgeConfig, MpdMprisBridge
from mpdris2.cover import DEFAULT_COVER_CACHE_DIR, DEFAULT_COVER_REGEX
from mpdris2.mpd_client import is_unix_socket
from mpdris2.notify import Notifier, NotifierConfig, NotifyTemplates

logger = logging.getLogger("mpdris2")

BUS_CONNECT_TIMEOUT = 10.0

# Bind the message catalog so ``from gettext import gettext as _``
# lookups in bridge.py / notify.py hit our installed .mo files.
# Catalogs ship as package data under
# ``mpdris2/locale/<lang>/LC_MESSAGES/mpdris2.mo``.
_LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
gettext.bindtextdomain("mpdris2", _LOCALE_DIR)
gettext.textdomain("mpdris2")

CONFIG_PATHS = [
    os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "mpDris2",
        "mpDris2.conf",
    ),
    "/etc/mpDris2/mpDris2.conf",
]


class ConfigError(Exception):
    """Raised when the daemon can't start because of invalid / missing config."""


def read_config(path: str | None = None) -> configparser.ConfigParser:
    """Parse the first existing INI file (or ``path`` if given).

    Sections preserved from the original mpDris2 layout:
    ``[Connection]`` / ``[Library]`` / ``[Bling]`` / ``[Notify]``.
    Missing file is not an error — defaults apply.
    """
    cfg = configparser.ConfigParser()
    paths = [path] if path else CONFIG_PATHS
    for p in paths:
        if p and os.path.exists(p):
            cfg.read(p)
            logger.info("read %s", p)
            return cfg
    logger.info("no config file found, using defaults")
    return cfg


def _resolve_notify(cfg: configparser.ConfigParser) -> bool:
    # [Notify] preferred, fall back to deprecated [Bling].
    return cfg.getboolean(
        "Notify", "notify",
        fallback=cfg.getboolean("Bling", "notification", fallback=True),
    )


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


def _resolve_cdprev(cfg: configparser.ConfigParser) -> bool:
    return cfg.getboolean("Bling", "cdprev", fallback=False)


def build_bridge_config(
    cfg: configparser.ConfigParser, args: argparse.Namespace,
) -> BridgeConfig:
    host, port, password = _resolve_endpoint(cfg, args)
    is_socket = is_unix_socket(host)
    return BridgeConfig(
        host=host,
        port=port,
        password=password,
        is_socket=is_socket,
        music_dir=_resolve_music_dir(cfg, args, socket=is_socket),
        cover_regex=_resolve_cover_regex(cfg),
        cover_cache_dir=_resolve_cover_cache_dir(cfg),
        cdprev=_resolve_cdprev(cfg),
        notify_paused=_resolve_notify_paused(cfg),
        no_reconnect=args.no_reconnect,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mpDris2",
        description="MPRIS2 D-Bus bridge for MPD.",
    )
    p.add_argument("-v", "--verbose", action="store_true",
                   help="enable debug logging")
    p.add_argument("--config", metavar="PATH",
                   help="path to an alternative config file")
    p.add_argument("--use-journal", action="store_true",
                   help="log to systemd journal (no timestamps in stderr)")
    p.add_argument("--no-reconnect", action="store_true",
                   help="exit instead of reconnecting if MPD disconnects")
    p.add_argument("-H", "--host", metavar="HOST",
                   help="MPD host (overrides [Connection] host)")
    p.add_argument("-p", "--port", metavar="PORT", type=int,
                   help="MPD port (overrides [Connection] port)")
    p.add_argument("--music-dir", metavar="PATH",
                   help="music library path (overrides [Library] music_dir)")
    return p


def main() -> None:
    args = build_parser().parse_args()

    log_format = ("%(levelname)s: %(name)s - %(message)s"
                  if args.use_journal
                  else "%(asctime)s %(levelname)s: %(name)s - %(message)s")
    logging.basicConfig(
        format=log_format,
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    try:
        cfg = read_config(args.config)
    except (OSError, configparser.Error) as e:
        logger.critical("failed to read config: %s", e)
        sys.exit(1)

    bridge_config = build_bridge_config(cfg, args)

    async def _amain() -> None:
        # SIGTERM / SIGINT cancel the daemon task; CancelledError
        # propagates through all the awaits (notably ``client.idle()``)
        # so cleanup runs immediately instead of waiting for the next
        # MPD event.
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        loop.add_signal_handler(signal.SIGINT, task.cancel)

        try:
            async with asyncio.timeout(BUS_CONNECT_TIMEOUT):
                bus = await MessageBus(bus_type=BusType.SESSION).connect()
        except TimeoutError:
            logger.critical(
                "D-Bus session bus did not respond within %.0fs; aborting",
                BUS_CONNECT_TIMEOUT,
            )
            raise

        notifier = Notifier(
            bus, app_name="mpDris2",
            config=_resolve_notifier_config(cfg),
            templates=_resolve_notify_templates(cfg),
        ) if _resolve_notify(cfg) else None

        bridge = MpdMprisBridge(bridge_config, bus=bus, notifier=notifier)
        try:
            await bridge.setup()
            await bridge.run_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await bridge.close()
            with contextlib.suppress(Exception):
                bus.disconnect()

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
