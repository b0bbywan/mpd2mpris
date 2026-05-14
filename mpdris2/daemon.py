"""Daemon orchestration — asyncio runtime that wires MPD, D-Bus, cover,
and notifications together.

PR 1 ships a placeholder ``run`` coroutine that just waits for SIGTERM /
SIGINT so the new entry point can be exercised end-to-end (``pip install
-e .`` then ``mpDris2 -v``) before any of the real wrappers exist.
PR 2 will replace this with the MPD + D-Bus glue.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import logging
import signal

logger = logging.getLogger(__name__)


async def run(cfg: configparser.ConfigParser, args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    logger.info("mpDris2 started (skeleton — D-Bus + MPD wiring pending)")

    stop_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down")
