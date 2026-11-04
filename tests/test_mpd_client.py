"""Unit tests for mpd_client.fetch_config, against an unconnected client."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import mpd
import pytest
from mpd.asyncio import MPDClient

from mpd2mpris import mpd_client
from mpd2mpris.mpd_client import fetch_config


@pytest.mark.asyncio
async def test_fetch_config_uses_the_command_from_3_1_2(monkeypatch):
    monkeypatch.setattr(mpd, "VERSION", (3, 1, 2))
    client = MPDClient()
    client.config = AsyncMock(return_value={"music_directory": "/srv/music"})  # type: ignore[method-assign]

    assert await fetch_config(client) == {"music_directory": "/srv/music"}


@pytest.mark.asyncio
async def test_fetch_config_gives_up_after_the_timeout(monkeypatch):
    monkeypatch.setattr(mpd, "VERSION", (3, 1, 2))
    monkeypatch.setattr(mpd_client, "CONFIG_PROBE_TIMEOUT", 0.01)
    client = MPDClient()

    async def _never_answers() -> dict[str, str]:
        await asyncio.sleep(1)
        return {}

    client.config = _never_answers  # type: ignore[method-assign]

    assert await fetch_config(client) == {}


@pytest.mark.asyncio
async def test_fetch_config_parses_every_pair_before_3_1_2(monkeypatch):
    monkeypatch.setattr(mpd, "VERSION", (3, 1, 1))
    client = MPDClient()
    queue: asyncio.Queue = asyncio.Queue()
    written: list = []

    def _write_command(command: str, args: list[Any]) -> None:
        written.append((command, args))

    client._MPDClient__command_queue = queue  # type: ignore[attr-defined]
    client._end_idle = lambda: None  # type: ignore[method-assign]
    client._write_command = _write_command  # type: ignore[method-assign]

    task = asyncio.create_task(fetch_config(client))
    result = await queue.get()
    for line in ("music_directory: /srv/music", "pcre: 1", None):
        result._feed_line(line)

    assert await task == {"music_directory": "/srv/music", "pcre": "1"}
    assert written == [("config", [])]
