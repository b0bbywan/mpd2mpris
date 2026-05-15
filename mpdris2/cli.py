"""CLI entry point: argparse + config loading + asyncio dispatch.

Kept separate from the daemon runtime (``mpdris2.daemon``) so the
bootstrap surface (argument parsing, config resolution) is testable in
isolation from the asyncio event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import logging
import os
import signal
import sys

logger = logging.getLogger("mpdris2")

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

    # Imported lazily so test_cli.py can exercise main() without dragging
    # in dbus-fast / python-mpd2 at import time.
    from mpdris2.bridge import MpdMprisBridge

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

        bridge = MpdMprisBridge(cfg, args)
        await bridge.setup()
        try:
            await bridge.run_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await bridge.close()

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
