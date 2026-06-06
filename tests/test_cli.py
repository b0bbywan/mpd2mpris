"""Argparse + config-loading tests. No D-Bus, no MPD, no event loop."""

from __future__ import annotations

import argparse
import configparser
from pathlib import Path

from mpdris2.cli import (
    _resolve_cdprev,
    _resolve_cover_backend,
    _resolve_music_dir,
    _resolve_mympd_uri,
    build_parser,
    read_config,
)


def _ns(**overrides) -> argparse.Namespace:
    base = {"music_dir": None, "host": None, "port": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_parser_defaults() -> None:
    args = build_parser().parse_args([])
    assert args.verbose is False
    assert args.config is None
    assert args.use_journal is False
    assert args.no_reconnect is False
    assert args.host is None
    assert args.port is None
    assert args.music_dir is None


def test_parser_flags() -> None:
    args = build_parser().parse_args([
        "-v",
        "--use-journal",
        "--no-reconnect",
        "-H", "192.0.2.10",
        "-p", "6601",
        "--music-dir", "/srv/music",
    ])
    assert args.verbose is True
    assert args.use_journal is True
    assert args.no_reconnect is True
    assert args.host == "192.0.2.10"
    assert args.port == 6601
    assert args.music_dir == "/srv/music"


def test_read_config_missing_file_uses_defaults(tmp_path: Path) -> None:
    # Point at a path that doesn't exist; parser returns an empty
    # ConfigParser instead of raising.
    cfg = read_config(str(tmp_path / "absent.conf"))
    assert cfg.sections() == []


def test_read_config_parses_ini(tmp_path: Path) -> None:
    p = tmp_path / "mpDris2.conf"
    p.write_text(
        "[Connection]\n"
        "host = mpd.example\n"
        "port = 6600\n"
        "\n"
        "[Library]\n"
        "music_dir = /srv/music\n"
    )
    cfg = read_config(str(p))
    assert cfg.get("Connection", "host") == "mpd.example"
    assert cfg.getint("Connection", "port") == 6600
    assert cfg.get("Library", "music_dir") == "/srv/music"


def test_read_config_no_argument_empty_when_no_file(tmp_path: Path, monkeypatch) -> None:
    # No arg → search the default paths; nothing present → empty parser, no raise.
    monkeypatch.setattr("mpdris2.cli._config_paths", lambda: [tmp_path / "absent.conf"])
    cfg = read_config(None)
    assert cfg.sections() == []


def test_read_config_honours_xdg_config_home(tmp_path: Path, monkeypatch) -> None:
    # XDG_CONFIG_HOME is resolved live, and its file is found before the
    # system fallback (it's first in the search order).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfgfile = tmp_path / "mpDris2" / "mpDris2.conf"
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text("[Connection]\nhost = xdg.example\n")
    cfg = read_config(None)
    assert cfg.get("Connection", "host") == "xdg.example"


# --- _resolve_music_dir ----------------------------------------------------

def test_resolve_music_dir_from_cli(tmp_path: Path) -> None:
    args = _ns(music_dir=str(tmp_path))
    cfg = configparser.ConfigParser()
    assert _resolve_music_dir(cfg, args) == tmp_path


def test_resolve_music_dir_from_file_uri_in_config() -> None:
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = file:///srv/music\n")
    assert _resolve_music_dir(cfg, args) == Path("/srv/music")


def test_resolve_music_dir_expands_tilde() -> None:
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = ~/Music\n")
    result = _resolve_music_dir(cfg, args)
    assert result == Path.home() / "Music"


def test_resolve_music_dir_non_local_scheme_returns_none(caplog) -> None:
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = http://example.com/music\n")
    with caplog.at_level("WARNING"):
        assert _resolve_music_dir(cfg, args) is None
    assert any("absolute" in r.message for r in caplog.records)


def test_resolve_music_dir_relative_path_returns_none(caplog) -> None:
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = Music\n")
    with caplog.at_level("WARNING"):
        assert _resolve_music_dir(cfg, args) is None
    assert any("absolute" in r.message for r in caplog.records)


def test_resolve_music_dir_file_uri_with_relative_path_rejected(caplog) -> None:
    """``file://relative`` is invalid per RFC 8089 and would crash later
    in ``Path.as_uri()`` — reject up front."""
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string("[Library]\nmusic_dir = file://Music\n")
    with caplog.at_level("WARNING"):
        assert _resolve_music_dir(cfg, args) is None


def test_resolve_music_dir_unset_returns_none() -> None:
    args = _ns()
    cfg = configparser.ConfigParser()
    assert _resolve_music_dir(cfg, args) is None


# --- _resolve_cdprev -------------------------------------------------------

def test_resolve_cdprev_default_false() -> None:
    assert _resolve_cdprev(configparser.ConfigParser()) is False


def test_resolve_cdprev_explicit_true() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Bling]\ncdprev = True\n")
    assert _resolve_cdprev(cfg) is True


# --- _resolve_cover_backend ------------------------------------------------

def test_resolve_cover_backend_default_false() -> None:
    cfg = configparser.ConfigParser()
    assert _resolve_cover_backend(cfg, "itunes") is False
    assert _resolve_cover_backend(cfg, "deezer") is False


def test_resolve_cover_backend_explicit_true() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Cover]\nitunes = True\ndeezer = True\n")
    assert _resolve_cover_backend(cfg, "itunes") is True
    assert _resolve_cover_backend(cfg, "deezer") is True


# --- _resolve_mympd_uri ----------------------------------------------------

def test_resolve_mympd_uri_default_none() -> None:
    assert _resolve_mympd_uri(configparser.ConfigParser()) is None


def test_resolve_mympd_uri_explicit() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Cover]\nmympd_uri = http://host:8080\n")
    assert _resolve_mympd_uri(cfg) == "http://host:8080"


def test_resolve_mympd_uri_blank_is_none() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Cover]\nmympd_uri =\n")
    assert _resolve_mympd_uri(cfg) is None


def test_resolve_mympd_uri_strips_surrounding_quotes() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string('[Cover]\nmympd_uri = "http://localhost:8090"\n')
    assert _resolve_mympd_uri(cfg) == "http://localhost:8090"
