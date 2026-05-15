"""Argparse + config-loading tests. No D-Bus, no MPD, no event loop."""

from __future__ import annotations

import argparse
import configparser
import os
from pathlib import Path

from mpdris2.cli import (
    _find_xdg_music_dir,
    _resolve_cdprev,
    _resolve_music_dir,
    _resolve_notifier_config,
    _resolve_notify,
    _resolve_notify_paused,
    _resolve_notify_templates,
    build_parser,
    read_config,
)
from mpdris2.notify import NotifyTemplates


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


# --- notify resolvers ------------------------------------------------------

def test_resolve_notify_default_true() -> None:
    assert _resolve_notify(configparser.ConfigParser()) is True


def test_resolve_notify_explicit_false() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Notify]\nnotify = False\n")
    assert _resolve_notify(cfg) is False


def test_resolve_notify_falls_back_to_bling() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Bling]\nnotification = False\n")
    assert _resolve_notify(cfg) is False


def test_resolve_notify_paused_default_false() -> None:
    assert _resolve_notify_paused(configparser.ConfigParser()) is False


def test_resolve_notify_paused_explicit_true() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Bling]\nnotify_paused = True\n")
    assert _resolve_notify_paused(cfg) is True


def test_resolve_notify_templates_defaults_blank() -> None:
    t = _resolve_notify_templates(configparser.ConfigParser())
    assert t == NotifyTemplates()


def test_resolve_notify_templates_explicit() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string(
        "[Notify]\n"
        "summary = %title%\n"
        "body = by %artist%\n"
        "paused_summary = (paused) %title%\n"
        "paused_body = was %artist%\n"
    )
    t = _resolve_notify_templates(cfg)
    assert t.summary == "%title%"
    assert t.body == "by %artist%"
    assert t.paused_summary == "(paused) %title%"
    assert t.paused_body == "was %artist%"


def test_resolve_notifier_config_defaults() -> None:
    nc = _resolve_notifier_config(configparser.ConfigParser())
    assert nc.urgency == 1
    assert nc.timeout == -1


def test_resolve_notifier_config_explicit() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Notify]\nurgency = 2\ntimeout = 5000\n")
    nc = _resolve_notifier_config(cfg)
    assert nc.urgency == 2
    assert nc.timeout == 5000


def test_read_config_no_argument_falls_back_to_xdg(tmp_path: Path, monkeypatch) -> None:
    # Force the XDG path to point inside tmp_path so the lookup
    # is hermetic. With no file present the parser still returns an
    # empty ConfigParser rather than raising.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Re-import so the module-level CONFIG_PATHS would pick up XDG —
    # but CONFIG_PATHS is computed at import time, so this exercises the
    # caller-supplied None branch instead.
    cfg = read_config(None)
    assert cfg.sections() == []
    os.environ.pop("XDG_CONFIG_HOME", None)


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


def test_resolve_music_dir_xdg_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_MUSIC_DIR", str(tmp_path))
    args = _ns()
    cfg = configparser.ConfigParser()
    assert _resolve_music_dir(cfg, args) == tmp_path


def test_resolve_music_dir_socket_skips_xdg_fallback(
    monkeypatch, tmp_path: Path,
) -> None:
    """On socket connections the XDG fallback is skipped — MPD's
    ``config`` command will give us the music_directory authoritatively."""
    monkeypatch.setenv("XDG_MUSIC_DIR", str(tmp_path))
    args = _ns()
    cfg = configparser.ConfigParser()
    assert _resolve_music_dir(cfg, args, socket=True) is None


def test_resolve_music_dir_socket_still_uses_explicit_config(
    tmp_path: Path,
) -> None:
    """Explicit config wins over socket auto-detect: user opted in."""
    args = _ns()
    cfg = configparser.ConfigParser()
    cfg.read_string(f"[Library]\nmusic_dir = {tmp_path}\n")
    assert _resolve_music_dir(cfg, args, socket=True) == tmp_path


# --- _find_xdg_music_dir ---------------------------------------------------

def test_find_xdg_music_dir_env_var(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_MUSIC_DIR", str(tmp_path))
    assert _find_xdg_music_dir() == tmp_path


def test_find_xdg_music_dir_user_dirs_dollar_home(
    monkeypatch, tmp_path: Path,
) -> None:
    cfg_home = tmp_path / "config"
    cfg_home.mkdir()
    (cfg_home / "user-dirs.dirs").write_text('XDG_MUSIC_DIR="$HOME/MyMusic"\n')
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() resolves HOME at call time → fakehome.
    assert _find_xdg_music_dir() == tmp_path / "MyMusic"


def test_find_xdg_music_dir_user_dirs_absolute(
    monkeypatch, tmp_path: Path,
) -> None:
    cfg_home = tmp_path / "config"
    cfg_home.mkdir()
    (cfg_home / "user-dirs.dirs").write_text(
        f'XDG_MUSIC_DIR="{tmp_path}/abs_music"\n'
    )
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    assert _find_xdg_music_dir() == tmp_path / "abs_music"


def test_find_xdg_music_dir_directory_fallback(
    monkeypatch, tmp_path: Path,
) -> None:
    # No env vars, no user-dirs file — fall back to ~/Music if it exists.
    (tmp_path / "Music").mkdir()
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _find_xdg_music_dir() == tmp_path / "Music"


def test_find_xdg_music_dir_no_match(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    assert _find_xdg_music_dir() is None


# --- _resolve_cdprev -------------------------------------------------------

def test_resolve_cdprev_default_false() -> None:
    assert _resolve_cdprev(configparser.ConfigParser()) is False


def test_resolve_cdprev_explicit_true() -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string("[Bling]\ncdprev = True\n")
    assert _resolve_cdprev(cfg) is True
