# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

mpDris2 is a Python 3 asyncio daemon that provides MPRIS 2 (Media Player Remote Interfacing Specification) D-Bus interface support for MPD (Music Player Daemon). It monitors a local or remote MPD server and exposes it as an MPRIS2-compliant media player on the session D-Bus, using `python-mpd2` for the MPD protocol and `dbus-fast` for the MPRIS interface. No threads, no GLib.

## Build System

`pyproject.toml` (setuptools backend) is the build entry point; `mpdris2/__init__.py` is the version source of truth.

```bash
# Dev install + tooling
pip install -e '.[dev,cover]'

# Lint, type-check, tests
make lint
make test

# Build the Debian package (needs a Debian toolchain — use the
# debian:trixie container that CI uses for parity).
make deb

# Sync the .deb changelog with __init__.py before tagging a release
make sync-deb
```

For Nix users, `shell.nix` provides a development shell.

## Source Structure

Flat package at `mpdris2/`:

| Module | Responsibility |
|--------|----------------|
| `__init__.py` | `__version__` (parsed by `scripts/version.py` and `pyproject.toml`) |
| `__main__.py` | `python -m mpdris2` entry point |
| `cli.py` | argparse, INI config loading, gettext bind, `asyncio.run(run(cfg, args))` |
| `bridge.py` | `MpdMprisBridge` — MPD connect/reconnect, D-Bus export, MPRIS callbacks, idle-driven `refresh()` |
| `mpd_client.py` | `mpd.asyncio.MPDClient` wrapper: connect-with-backoff + capability probe |
| `mpris.py` | `dbus_fast.ServiceInterface` classes: `MediaPlayer2` (root) + `MediaPlayer2Player` |
| `translate.py` | Pure MPD song dict → MPRIS Metadata dict (`Variant`-wrapped) |
| `cover.py` | 5-step async cover pipeline (MPD readpicture → filesystem regex → MPD albumart → CUE/cdda fallback → XDG cache) |
| `notify.py` | Desktop notifications via `org.freedesktop.Notifications` over dbus-fast |
| `locale/` | Compiled `.mo` files (built from `po/*.po`, shipped as package data) |

Helper scripts: `scripts/version.py` parses `__init__.py` and produces both PEP 440 and Debian-sortable forms. Used by `make sync-deb` and `make check-tag`.

## Runtime Dependencies

- Python 3.11+
- `python-mpd2 >= 3.1`
- `dbus-fast >= 2.0`

Dev: `pytest`, `pytest-asyncio`, `mypy`, `ruff`, `babel`, `build`.

## Configuration

User config at `~/.config/mpDris2/mpDris2.conf` (INI), falling back to `/etc/mpDris2/mpDris2.conf`. Example shipped at `/usr/share/doc/mpdris2/mpdris2.conf`.

Sections in current use:
- `[Connection]` — `host`, `port`, `password`
- `[Library]` — `music_dir`, `cover_regex`
- `[Notify]` — `notify` (bool)

CLI overrides config: `-H`/`--host`, `-p`/`--port`, `--music-dir`, `--config`, `--use-journal`, `--no-reconnect`, `-v`/`--verbose`.

## i18n

`po/fr.po` + `po/nl.po`. `babel.cfg` controls extraction, `msgfmt` (from `gettext`) compiles `.mo` files.

```bash
make i18n-extract   # refresh po/mpdris2.pot from current source
make i18n-compile   # rebuild mpdris2/locale/*/LC_MESSAGES/mpdris2.mo
```

The runtime catalog lookup is wired in `cli.py` (`gettext.bindtextdomain` + `gettext.textdomain` against `mpdris2/locale/`). Modules use `from gettext import gettext as _`.

## Packaging

- **Debian**: `debian/` uses `pybuild-plugin-pyproject` (no autotools). `debian/rules` calls `msgfmt` before `dh_auto_build` to compile `.mo` files; data files (`data/*.{service,desktop,conf}`) are listed in `debian/mpdris2.install`.
- **Systemd**: user unit `data/user/mpDris2.service` (`Type=dbus`, `BusName=org.mpris.MediaPlayer2.mpd`, `Restart=on-failure`, `ConditionUser=!root` / `ConditionUser=!@system`). Not auto-enabled on install — D-Bus activation kicks it in on the first MPRIS call.
- **D-Bus activation**: `data/dbus-1/org.mpris.MediaPlayer2.mpd.service` → `/usr/share/dbus-1/services/`.
- **CI**: `.github/workflows/build.yml` runs lint + tests on every PR, builds the `.deb` in a `debian:trixie` container on tags, creates a GitHub release, and dispatches to the private `b0bbywan/odio-apt-repo` for APT-repo rebuild.

## Notes

- Media keys: the original GNOME `org.gnome.SettingsDaemon.MediaKeys` grab was dropped during the asyncio rewrite. Modern desktops (GNOME ≥ 3.6, KDE, Sway/Hyprland with `playerctld`) route media keys through MPRIS2 directly.
- The `[Bling] mmkeys` config key is no longer honoured (the GNOME MediaKeys grab was dropped in the asyncio rewrite). `[Bling] cdprev` and `[Bling] notify_paused` are still honoured by `bridge.py`.
- The legacy `src/mpDris2.in.py` + autotools build was removed in the same refactor; see `docs/refactor-asyncio-dbus-fast.md` for the migration plan.
