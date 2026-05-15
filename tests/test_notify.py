"""Unit tests for the pure helpers in notify.py — formatter + duration.

The Notifier class itself needs a live D-Bus, so it's exercised via
the bridge integration tests, not here.
"""

from __future__ import annotations

import pytest
from dbus_fast import Variant

from mpdris2.notify import (
    NotifyTemplates,
    _build_track_notification,
    _format_duration,
    _icon_path_for,
    format_template,
)


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


# --- _icon_path_for --------------------------------------------------------

def test_icon_path_for_file_uri_strips_scheme() -> None:
    meta = {"mpris:artUrl": Variant("s", "file:///tmp/cover.jpg")}
    assert _icon_path_for(meta) == "/tmp/cover.jpg"


def test_icon_path_for_plain_path_unchanged() -> None:
    meta = {"mpris:artUrl": Variant("s", "/tmp/cover.jpg")}
    assert _icon_path_for(meta) == "/tmp/cover.jpg"


def test_icon_path_for_missing() -> None:
    assert _icon_path_for({}) == ""


# --- _build_track_notification --------------------------------------------

def test_track_notification_full_meta() -> None:
    meta = {
        "xesam:title": Variant("s", "Song"),
        "xesam:artist": Variant("as", ["Artist A", "Artist B"]),
        "mpris:artUrl": Variant("s", "file:///tmp/c.jpg"),
    }
    title, body, icon = _build_track_notification(meta)
    assert title == "Song"
    assert body == "by Artist A, Artist B"
    assert icon == "/tmp/c.jpg"


def test_track_notification_missing_fields() -> None:
    title, body, icon = _build_track_notification({})
    assert title == "Unknown title"
    assert body == "by Unknown artist"
    assert icon == ""


def test_track_notification_paused_default_appends_marker() -> None:
    meta = {"xesam:title": Variant("s", "Song"),
            "xesam:artist": Variant("as", ["A"])}
    title, body, icon = _build_track_notification(meta, state="pause")
    assert title == "Song"
    assert body == "by A (Paused)"
    assert icon == "media-playback-pause-symbolic"


def test_track_notification_summary_template_expands() -> None:
    meta = {
        "xesam:title": Variant("s", "Song"),
        "xesam:album": Variant("s", "Album"),
        "xesam:artist": Variant("as", ["A"]),
    }
    templates = NotifyTemplates(summary="%artist% — %title%", body="from %album%")
    title, body, _icon = _build_track_notification(meta, templates=templates)
    assert title == "A — Song"
    assert body == "from Album"


def test_track_notification_paused_template_falls_back_to_playing() -> None:
    # No paused_summary → uses the playing template; no paused_body → same.
    meta = {"xesam:title": Variant("s", "Song"),
            "xesam:artist": Variant("as", ["A"])}
    templates = NotifyTemplates(summary="P:%title%", body="B:%artist%")
    title, body, icon = _build_track_notification(meta, state="pause", templates=templates)
    assert title == "P:Song"
    assert body == "B:A"
    assert icon == "media-playback-pause-symbolic"


def test_track_notification_paused_uses_paused_templates_when_set() -> None:
    meta = {"xesam:title": Variant("s", "Song"),
            "xesam:artist": Variant("as", ["A"])}
    templates = NotifyTemplates(
        summary="P:%title%", body="B:%artist%",
        paused_summary="zzz", paused_body="snoring",
    )
    title, body, _icon = _build_track_notification(meta, state="pause", templates=templates)
    assert title == "zzz"
    assert body == "snoring"
