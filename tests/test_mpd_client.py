"""Unit tests for mpd_client.fetch_config — no MPD, no socket.

``fetch_config`` has two paths: on python-mpd2 >= 3.1.2 ``client.config()``
returns a parsed dict directly; on older releases (e.g. Debian stable's
3.1.1) it mis-parses the multi-pair response and returns ``None``, so we
re-issue ``config`` through python-mpd2's internal command queue with a
correct dict parser. Both branches are exercised here by controlling the
mocked ``client.config()`` return value, independent of the installed
python-mpd2 version. A real ``MPDClient`` instance is used (never
connected) so the fallback's ``client._parse_pairs`` does real parsing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import mpd
import pytest
from mpd.asyncio import MPDClient

from mpdris2.mpd_client import fetch_config


@pytest.mark.asyncio
async def test_fetch_config_native_returns_dict() -> None:
    # python-mpd2 >= 3.1.2: config() already yields a dict.
    client = MagicMock()
    client.config = AsyncMock(
        return_value={"music_directory": "/srv/music", "pcre": "1"}
    )

    assert await fetch_config(client) == {
        "music_directory": "/srv/music",
        "pcre": "1",
    }
    client.config.assert_awaited_once()
    # The native path must not touch the private-API fallback.
    client._write_command.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_config_fallback_parses_multipair() -> None:
    # python-mpd2 < 3.1.2: config() mis-parses the multi-pair answer as a
    # single item and returns None, triggering the private-queue fallback.
    client = MPDClient()
    client.config = AsyncMock(return_value=None)
    client._end_idle = MagicMock()
    client._write_command = MagicMock()

    # Capture the CommandResult the fallback enqueues and drive it as the
    # real read loop would: feed the raw config lines, then a None sentinel
    # that flushes them through the dict-parsing callback.
    async def _put(cmd_result: object) -> None:
        for line in ("music_directory: /srv/music",
                     "playlist_directory: /srv/pl", "pcre: 1"):
            cmd_result._feed_line(line)
        cmd_result._feed_line(None)

    queue = MagicMock()
    queue.put = AsyncMock(side_effect=_put)
    client._MPDClient__command_queue = queue

    assert await fetch_config(client) == {
        "music_directory": "/srv/music",
        "playlist_directory": "/srv/pl",
        "pcre": "1",
    }
    client._write_command.assert_called_once_with("config")


@pytest.mark.asyncio
async def test_fetch_config_returns_empty_on_connection_error() -> None:
    # A dropped connection during the probe is swallowed: the caller just
    # ends up without an auto-detected music_directory.
    client = MagicMock()
    client.config = AsyncMock(side_effect=mpd.ConnectionError("gone"))

    assert await fetch_config(client) == {}
