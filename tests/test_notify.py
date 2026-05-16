"""Unit tests for the pure helpers in notify.py — formatter + duration.

The Notifier class itself needs a live D-Bus, so it's exercised via
the bridge integration tests, not here.
"""

from __future__ import annotations

import pytest
from dbus_fast import Variant

from mpdris2.notify import _format_duration, format_template


@pytest.mark.parametrize("secs,expected", [
    (0, "0:00"),
    (-3, "0:00"),
    (5, "0:05"),
    (61, "1:01"),
    (60 * 59 + 59, "59:59"),
    (3600, "1:00:00"),
    (3661, "1:01:01"),
])
def test_format_duration(secs: float, expected: str) -> None:
    assert _format_duration(secs) == expected


def test_format_template_basic_placeholders() -> None:
    meta = {
        "xesam:title": Variant("s", "Song"),
        "xesam:album": Variant("s", "Album"),
        "xesam:artist": Variant("as", ["Artist A", "Artist B"]),
        "xesam:trackNumber": Variant("i", 3),
        "mpris:length": Variant("x", 245_000_000),
    }
    out = format_template(
        "%artist% — %title% (#%track% on %album%, %time%)",
        meta,
    )
    assert out == "Artist A, Artist B — Song (#3 on Album, 4:05)"


def test_format_template_unknown_placeholder_kept() -> None:
    out = format_template("%title%/%nope%", {"xesam:title": Variant("s", "S")})
    assert out == "S/%nope%"


def test_format_template_missing_fields_use_defaults() -> None:
    out = format_template("%album%/%artist%/%title%", {})
    assert out == "Unknown album/Unknown artist/Unknown title"


def test_format_template_position() -> None:
    out = format_template("%timeposition%", {}, position_us=65_000_000)
    assert out == "1:05"


def test_format_template_id_from_trackid_tail() -> None:
    out = format_template(
        "%id%", {"mpris:trackid": Variant("o", "/org/mpris/MediaPlayer2/Track/42")},
    )
    assert out == "42"


def test_format_template_file_from_url_tail() -> None:
    out = format_template(
        "%file%", {"xesam:url": Variant("s", "file:///srv/music/Artist/01.flac")},
    )
    assert out == "01.flac"
