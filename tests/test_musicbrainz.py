"""Unit tests for the optional MusicBrainz / Cover Art Archive module.

``musicbrainzngs`` is monkeypatched with a fake; the network is never hit.
"""

from __future__ import annotations

import pytest

from mpd2mpris import musicbrainz

_COVER = "https://coverartarchive.org/release/rel-1/front-500.jpg"


class _FakeMB:
    """Minimal musicbrainzngs stand-in: records calls and replays canned
    return values."""

    def __init__(
        self,
        rg_id: str | None = None,
        cover: str | None = None,
        recordings: list[dict] | None = None,
        groups: list[dict] | None = None,
    ) -> None:
        self._rg_id = rg_id
        self._cover = cover
        self._recordings = recordings or []
        self._groups = groups or []
        self.recording_query: dict | None = None
        self.group_query: dict | None = None

    def set_useragent(self, *_a: object) -> None:
        pass

    def search_recordings(self, **kwargs: object) -> dict:
        self.recording_query = kwargs
        return {"recording-list": self._recordings}

    def search_release_groups(self, **kwargs: object) -> dict:
        self.group_query = kwargs
        return {"release-group-list": self._groups}

    def get_release_group_image_list(self, rg_id: str) -> dict:
        assert rg_id == self._rg_id
        if self._cover is None:
            return {"images": []}  # no front cover for this release group
        return {"images": [{"front": True, "thumbnails": {"500": self._cover}, "image": self._cover}]}


def _group(title: str, primary: str = "Album", secondary: list[str] | None = None, rg_id: str = "rg-1") -> dict:
    g: dict = {"id": rg_id, "title": title, "primary-type": primary}
    if secondary:
        g["secondary-type-list"] = secondary
    return g


def _recording(artist: str, track: str, groups: list[dict] | None) -> dict:
    rec: dict = {"artist-credit-phrase": artist, "title": track}
    if groups is not None:
        rec["release-list"] = [{"id": f"rel-{i}", "release-group": g} for i, g in enumerate(groups)]
    return rec


# --- resolve_album --------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_album_disabled_without_lib(monkeypatch) -> None:
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", None)
    assert await musicbrainz.resolve_album("Mato", "1980 Dub") is None


@pytest.mark.asyncio
async def test_resolve_album_empty_args_not_queried(monkeypatch) -> None:
    fake = _FakeMB()
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", fake)
    assert await musicbrainz.resolve_album("Station", "") is None
    assert fake.recording_query is None  # empty track → never queried


@pytest.mark.asyncio
async def test_resolve_album_fielded_query_and_validation(monkeypatch) -> None:
    fake = _FakeMB(recordings=[_recording("Yaniss Odua & FNX", "One Love", [_group("Umanizm")])])
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", fake)
    assert await musicbrainz.resolve_album("Yaniss Odua & Fnx", "One Love") == ("Yaniss Odua & FNX", "Umanizm")
    assert fake.recording_query == {"artist": "Yaniss Odua & Fnx", "recording": "One Love", "limit": 5}


@pytest.mark.asyncio
async def test_resolve_album_prefers_album_over_compilation(monkeypatch) -> None:
    rec = _recording("Bob Marley", "Jamming", [
        _group("Acoustic Jams", primary="Album", secondary=["Compilation"], rg_id="comp"),
        _group("Exodus", primary="Album", rg_id="studio"),
    ])
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", _FakeMB(recordings=[rec]))
    assert await musicbrainz.resolve_album("Bob Marley", "Jamming") == ("Bob Marley", "Exodus")


@pytest.mark.asyncio
async def test_resolve_album_matches_and_vs_ampersand(monkeypatch) -> None:
    # ICY "And" vs MB "&" must still match (the real Bob Marley failure).
    rec = _recording("Bob Marley & The Wailers", "Don't Rock My Boat", [_group("Soul Revolution")])
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", _FakeMB(recordings=[rec]))
    result = await musicbrainz.resolve_album("Bob Marley And The Wailers", "Don't Rock My Boat")
    assert result == ("Bob Marley & The Wailers", "Soul Revolution")


@pytest.mark.asyncio
async def test_resolve_album_matches_despite_accents(monkeypatch) -> None:
    fake = _FakeMB(recordings=[_recording("Téléphone", "Cendrillon", [_group("Crache ton venin")])])
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", fake)
    assert await musicbrainz.resolve_album("Telephone", "Cendrillon") == ("Téléphone", "Crache ton venin")


@pytest.mark.asyncio
async def test_resolve_album_rejects_substring_title(monkeypatch) -> None:
    # "Sunshine" must not match "Ain't No Sunshine [Ft. Sting]" by containment.
    fake = _FakeMB(recordings=[_recording("Shaggy", "Sunshine", [_group("Lessons for Beginners")])])
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", fake)
    assert await musicbrainz.resolve_album("Shaggy", "Ain't No Sunshine [Ft. Sting]") is None


@pytest.mark.asyncio
async def test_resolve_album_strips_featuring_decoration(monkeypatch) -> None:
    # The bracketed "[Ft. Sting]" is dropped, so the clean titles match.
    fake = _FakeMB(recordings=[_recording("Shaggy", "Ain't No Sunshine", [_group("Hot Shot")])])
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", fake)
    assert await musicbrainz.resolve_album("Shaggy", "Ain't No Sunshine [Ft. Sting]") == ("Shaggy", "Hot Shot")


@pytest.mark.asyncio
async def test_resolve_album_rejects_mismatched_hit(monkeypatch) -> None:
    # The query parses fine, but MB returns something unrelated; validation
    # must reject it.
    fake = _FakeMB(recordings=[_recording("Steinbruchel", "Seam", [_group("Seam")])])
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", fake)
    assert await musicbrainz.resolve_album("AUTOPROMO", "Twittos") is None


@pytest.mark.asyncio
async def test_resolve_album_skips_hit_without_release(monkeypatch) -> None:
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", _FakeMB(recordings=[_recording("Mato", "Dub", None)]))
    assert await musicbrainz.resolve_album("Mato", "Dub") is None


# --- cover_url ------------------------------------------------------------

@pytest.mark.asyncio
async def test_cover_url_disabled_without_lib(monkeypatch) -> None:
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", None)
    assert await musicbrainz.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_returns_url(monkeypatch) -> None:
    fake = _FakeMB("rg-1", _COVER, groups=[_group("B")])
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", fake)
    assert await musicbrainz.cover_url("A", "B") == _COVER
    assert fake.group_query == {"artist": "A", "releasegroup": "B", "limit": 5}


@pytest.mark.asyncio
async def test_cover_url_no_group(monkeypatch) -> None:
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", _FakeMB(groups=[]))
    assert await musicbrainz.cover_url("A", "B") is None


@pytest.mark.asyncio
async def test_cover_url_no_front_image(monkeypatch) -> None:
    monkeypatch.setattr(musicbrainz, "musicbrainzngs", _FakeMB("rg-1", None, groups=[_group("B")]))
    assert await musicbrainz.cover_url("A", "B") is None
