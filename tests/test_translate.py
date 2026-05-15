"""Pure-function tests for mpd_to_mpris — no D-Bus, no MPD."""

from __future__ import annotations

from pathlib import Path

from mpdris2.translate import mpd_to_mpris


def test_empty_song_returns_empty_dict() -> None:
    assert mpd_to_mpris({}) == {}


def test_basic_tags() -> None:
    m = mpd_to_mpris({
        "title": "Song",
        "album": "Album",
        "artist": "Artist",
        "albumartist": "AA",
        "composer": "C",
        "genre": "Pop",
        "id": "42",
        "track": "3",
        "disc": "2",
        "duration": "245.123",
        "date": "2023-06-15",
        "file": "Artist/Album/03 - Song.mp3",
    }, music_dir=Path("/srv/music"))
    assert m["xesam:title"].value == "Song"
    assert m["xesam:album"].value == "Album"
    assert m["xesam:artist"].value == ["Artist"]
    assert m["xesam:albumArtist"].value == ["AA"]
    assert m["xesam:composer"].value == ["C"]
    assert m["xesam:genre"].value == ["Pop"]
    assert m["mpris:trackid"].value == "/org/mpris/MediaPlayer2/Track/42"
    assert m["mpris:trackid"].signature == "o"
    assert m["xesam:trackNumber"].value == 3
    assert m["xesam:discNumber"].value == 2
    assert m["mpris:length"].value == 245_123_000  # microseconds
    assert m["xesam:contentCreated"].value == "2023"
    # music_dir prepended for relative paths (URL-encoded by as_uri)
    assert m["xesam:url"].value == "file:///srv/music/Artist/Album/03%20-%20Song.mp3"


def test_multi_artist_list_preserved() -> None:
    m = mpd_to_mpris({"artist": ["A", "B", "C"]})
    assert m["xesam:artist"].value == ["A", "B", "C"]
    assert m["xesam:artist"].signature == "as"


def test_artist_backfilled_from_albumartist() -> None:
    # CDDA / CUE tracks frequently expose only ``albumartist``.
    m = mpd_to_mpris({"albumartist": "AA", "title": "T"})
    assert m["xesam:artist"].value == ["AA"]
    assert m["xesam:albumArtist"].value == ["AA"]


def test_artist_not_overwritten_when_present() -> None:
    m = mpd_to_mpris({"artist": "Track Artist", "albumartist": "Album Artist"})
    assert m["xesam:artist"].value == ["Track Artist"]
    assert m["xesam:albumArtist"].value == ["Album Artist"]


def test_track_with_total_only_keeps_leading_int() -> None:
    # "3/12" is a common MPD format meaning track 3 of 12.
    m = mpd_to_mpris({"track": "3/12"})
    assert m["xesam:trackNumber"].value == 3


def test_url_with_scheme_left_untouched() -> None:
    m = mpd_to_mpris(
        {"file": "http://stream.example/live.mp3"},
        music_dir=Path("/srv/music"),
    )
    assert m["xesam:url"].value == "http://stream.example/live.mp3"


def test_stream_name_fills_missing_title() -> None:
    m = mpd_to_mpris({"name": "Radio Example", "file": "http://r/x.mp3"})
    assert m["xesam:title"].value == "Radio Example"


def test_stream_name_fills_album_when_title_present() -> None:
    m = mpd_to_mpris({
        "name": "Radio Example",
        "title": "Song - Artist",
    })
    assert m["xesam:title"].value == "Song - Artist"
    assert m["xesam:album"].value == "Radio Example"


def test_duration_takes_precedence_over_time() -> None:
    # MPD ships both; ``duration`` is the float-precision modern one.
    m = mpd_to_mpris({"time": "180", "duration": "180.456"})
    assert m["mpris:length"].value == 180_456_000


def test_unparseable_track_dropped_silently() -> None:
    m = mpd_to_mpris({"track": "garbage"})
    assert "xesam:trackNumber" not in m


def test_no_duration_no_length_key() -> None:
    m = mpd_to_mpris({"title": "x"})
    assert "mpris:length" not in m


def test_invalid_date_dropped() -> None:
    m = mpd_to_mpris({"date": "n/a"})
    assert "xesam:contentCreated" not in m
