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
| `translate.py` | Pure MPD song dict → MPRIS Metadata dict (`Variant`-wrapped); also `split_title` (shared ``Artist - Track`` ICY-title parser used by the web-radio resolvers). |
| `cover.py` | 7-step async cover pipeline (MPD readpicture → filesystem regex → MPD albumart → CUE/cdda fallback → remote cover URL → web-radio station favicon → myMPD WebradioDB). Steps 1–4 yield local/embedded covers (bytes → tempfile, or a `file://`). Step 5 delegates to `musicbrainz.py`/`itunes.py`/`deezer.py` for a remote image **URL** served verbatim (no download, no disk cache — lighter, and we link rather than re-host), memoised per (artist, album); iTunes/Deezer are opt-in (`[Cover]` section, off by default), MusicBrainz/CAA always runs. For web radio (title only) the artist+album are recovered from MusicBrainz first, then Deezer (broader catalogue) when enabled. Step 6 falls back to the station favicon URL (`radiobrowser.py`) for http(s):// streams; step 7 is the opt-in myMPD WebradioDB cover (`mympd.py`, enabled via `[Cover] mympd_uri`). |
| `musicbrainz.py` | Optional MusicBrainz / Cover Art Archive lookups (isolates the `musicbrainzngs` dep): `resolve_album(title)` (fielded recording search + artist/title validation, for web radio) and `cover_url(artist, album)` (release-group front-cover URL via the CAA image list — no image download). No-op when the dep is absent. |
| `itunes.py` / `deezer.py` | No-auth, stdlib-only cover-art fallbacks (`cover_url(artist, album)` → image URL from the search JSON) tried after MusicBrainz/CAA when it has no image — CAA coverage is sparse for some content. **Opt-in, off by default** (`[Cover] itunes` / `deezer`). `deezer.py` also has `resolve_album(title)` (track search, web-radio key recovery after MusicBrainz; gated on the `deezer` opt-in). |
| `radiobrowser.py` | No-auth, stdlib-only last-resort for http(s):// web-radio streams: `station_icon(stream_url)` returns the station's favicon **URL** (cover.py step 6). Returned verbatim as `mpris:artUrl` — no download. |
| `mympd.py` | No-auth, stdlib-only opt-in fallback (cover.py step 7, `[Cover] mympd_uri`): `cover_url(base_url, stream_url)` POSTs `MYMPD_API_WEBRADIODB_RADIO_GET_BY_URI` to a myMPD instance and returns the WebradioDB `Image` **URL** verbatim — no download. No-op when `mympd_uri` is unset. |
| `notify.py` | Desktop notifications via `org.freedesktop.Notifications` over dbus-fast |
| `locale/` | Compiled `.mo` files (built from `po/*.po`, shipped as package data) |

Helper scripts: `scripts/version.py` parses `__init__.py` and produces both PEP 440 and Debian-sortable forms. Used by `make sync-deb` and `make check-tag`.

## Runtime Dependencies

- Python 3.11+
- `python-mpd2 >= 3.1`
- `dbus-fast >= 2.0`

Optional (`pip install '.[cover]'`, Debian `python3-musicbrainzngs` + `python3-rapidfuzz` Recommends) — enables the cover.py step-6 MusicBrainz lookup: `musicbrainzngs >= 0.7` (recording/release search) + `rapidfuzz >= 3.0` (fuzzy artist/title validation). The iTunes/Deezer fallbacks need no extra deps (stdlib).

Dev: `pytest`, `pytest-asyncio`, `mypy`, `ruff`, `babel`, `build`.

## Configuration

User config at `~/.config/mpDris2/mpDris2.conf` (INI), falling back to `/etc/mpDris2/mpDris2.conf`. Example shipped at `/usr/share/doc/mpdris2/mpdris2.conf`.

Sections in current use:
- `[Connection]` — `host`, `port`, `password`
- `[Library]` — `music_dir`, `cover_regex`
- `[Cover]` — `itunes`, `deezer` (bool, both default off): opt-in remote cover-URL fallbacks (cover.py step 5); `mympd_uri` (str, unset = off): myMPD base URL for the WebradioDB fallback (cover.py step 7)
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
