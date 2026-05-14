"""Argparse + config-loading tests. No D-Bus, no MPD, no event loop."""

from __future__ import annotations

import os
from pathlib import Path

from mpdris2.cli import build_parser, read_config


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
