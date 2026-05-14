# mpDris2 — Modernization: pyproject + asyncio + dbus-fast

## Context

Today the project is built with autotools, runs synchronously on the GLib
mainloop, and binds D-Bus via dbus-python — the original 2010-era stack.
We want to mirror the architecture chosen for `snapclientmpris` PR #2
(`asyncio-rewrite`): pyproject + setuptools + asyncio + dbus-fast +
ruff/mypy/pytest. The MusicBrainz cover fallback originally drafted in
`./musicbrainz-cover-fallback.md` is deferred until after the refactor —
on the new stack it collapses to a single `asyncio.to_thread` call
instead of a thread + `GLib.idle_add` dance.

User-confirmed choices:
- Keep i18n (`po/fr.po`, `po/nl.po`) but drop intltool; compile `.mo`
  files via `babel`.
- Drop the GNOME Settings Daemon MediaKeys grab (obsolete; modern
  desktops route media keys through MPRIS2 directly).
- Min Python: **3.11**.

## Reference

Target shape: `b0bbywan/snapclientmpris@asyncio-rewrite` (PR #2). The
package surface is small (`__init__.py`, `__main__.py`, `cli.py`,
`mpris.py`, `<daemon>.py`, `translate.py`) and the patterns transfer 1:1.

## Target layout

```
mpDris2/
├── pyproject.toml
├── Makefile                              # dev helpers only
├── README.md  LICENSE  NEWS  AUTHORS
├── babel.cfg
├── scripts/
│   └── version.py                        # parses mpdris2/__init__.py
├── mpdris2/
│   ├── __init__.py                       # __version__ source of truth
│   ├── __main__.py                       # `python -m mpdris2`
│   ├── cli.py                            # argparse + config + asyncio.run
│   ├── mpris.py                          # dbus-fast ServiceInterface (Root, Player)
│   ├── mpd_client.py                     # mpd.asyncio.MPDClient + idle + reconnect
│   ├── cover.py                          # 4-step async pipeline
│   ├── notify.py                         # libnotify via dbus-fast (no PyGObject)
│   ├── translate.py                      # MPD tags -> MPRIS dict (pure)
│   └── locale/                           # compiled .mo files (package data)
├── po/   fr.po  nl.po  mpdris2.pot
├── data/
│   ├── mpdris2.conf                      # example config
│   ├── mpdris2.desktop
│   ├── dbus-1/org.mpris.MediaPlayer2.mpd.service
│   ├── system/mpDris2.service
│   └── user/mpDris2.service
├── tests/
│   ├── test_cli.py
│   ├── test_translate.py
│   ├── test_cover.py
│   └── test_mpris.py
├── debian/   control  rules  changelog  copyright  mpdris2.install  source/format
└── .github/workflows/build.yml
```

Removed: `configure.ac`, `autogen.sh`, `Makefile.am`, `src/Makefile.am`,
`src/mpDris2.in.py`, `src/mpDris2.service.in`,
`src/org.mpris.MediaPlayer2.mpd.service.in`, `aclocal.m4`,
`autom4te.cache/`, `build-aux/`, `INSTALL`, `configure`, `configure~`,
`Makefile.in`, `po/Makefile.in.in`, `po/POTFILES.in`,
`mpdris2-0.9.1.tar.gz`.

## Migration map

### 1. Build & packaging

| Before | After |
|--------|-------|
| autotools + sed `@version@`/`@bindir@`/... in `.in.py` | `__version__` in `mpdris2/__init__.py`; pyproject `dynamic = ["version"]` |
| `intltool` + `po/Makefile.in.in` | `babel` extract/compile; gettext stdlib at runtime |
| Makefile `bindir`/`datadir` install paths | `console_scripts` → `/usr/bin/mpDris2`; data files via `debian/install` |
| `dh_autoreconf ./autogen.sh` in `debian/rules` | `dh ... --with python3 --buildsystem=pybuild` |

`pyproject.toml` (model: `snapclientmpris/pyproject.toml`):
```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "mpdris2"
description = "MPRIS2 D-Bus bridge for MPD"
readme = "README.md"
license = {text = "GPL-3.0-or-later"}
authors = [{name = "Mathieu Réquillart", email = "mathieu.requillart@gmail.com"}]
requires-python = ">=3.11"
dependencies = [
    "python-mpd2>=3.1",
    "dbus-fast>=2.0",
]
[project.optional-dependencies]
cover = ["mutagen>=1.45"]
musicbrainz = ["musicbrainzngs>=0.7.1"]
dev = ["pytest", "pytest-asyncio", "mypy", "ruff", "babel"]
dynamic = ["version"]

[project.scripts]
mpDris2 = "mpdris2.cli:main"

[tool.setuptools.dynamic]
version = {attr = "mpdris2.__version__"}
[tool.setuptools.packages.find]
include = ["mpdris2*"]
exclude = ["tests*"]
[tool.setuptools.package-data]
mpdris2 = ["locale/*/LC_MESSAGES/*.mo"]
```
ruff + mypy blocks copied from `snapclientmpris/pyproject.toml` (notably
the `mpris.py` overrides for `F821/F722/UP037` — D-Bus signature strings
as annotations confuse ruff).

### 2. CLI (`mpdris2/cli.py`)

Mirror `snapclientmpris/cli.py`. `argparse` with `-v/--verbose`,
`--use-journal`, `--no-reconnect`, `-h/--host`, `-p/--port`,
`--music-dir`, `--config`. Config from
`~/.config/mpDris2/mpDris2.conf` then `/etc/mpDris2/mpDris2.conf`
(keep the existing ConfigParser INI sections:
`[Connection]`/`[Library]`/`[Bling]`/`[Notify]`). End with
`asyncio.run(run(cfg))`.

### 3. D-Bus (`mpdris2/mpris.py`)

Two `dbus_fast.service.ServiceInterface` subclasses (model:
`snapclientmpris/mpris.py:36-296`):

- `MediaPlayer2` — root: `CanQuit`/`CanRaise`/`Identity`/`HasTrackList`
  (=False, matches current line 1230)/`SupportedUriSchemes`/
  `SupportedMimeTypes`/`Raise()`/`Quit()`.
- `MediaPlayer2Player` — playback state + controls: `PlaybackStatus`,
  `Metadata` (`a{sv}`), `Volume` (R/W), `Position`, `Rate`/`MinimumRate`/
  `MaximumRate`, `LoopStatus`, `Shuffle`, `Can*`; `Play`/`Pause`/
  `PlayPause`/`Stop`/`Next`/`Previous`/`Seek`/`SetPosition`/`OpenUri`,
  `Seeked` signal.

External callers push state in via `update_playback_status(...)`,
`update_metadata(...)`, `update_volume(...)`, `update_position(...)`,
`update_loop_status(...)`, `update_shuffle(...)` — each calls
`self.emit_properties_changed({...})`. Mirror the synchronous
emit-then-callback pattern from `snapclientmpris/mpris.py:230-248`.

### 4. MPD client (`mpdris2/mpd_client.py`)

Use `mpd.asyncio.MPDClient` (shipped by python-mpd2 since 3.0). Wrapper:
```python
class MPDClient:
    async def connect_with_retry(self): ...    # exponential backoff
    async def idle_loop(self, on_change): ...  # async for ev in client.idle(): ...
    async def status(self): ...
    async def currentsong(self): ...
    async def readpicture(self, uri): ...
    async def albumart(self, uri): ...
```
The compat shims at `src/mpDris2.in.py:1042-1100` (python-mpd 0.2,
`_write_command`, `_fetch_object`, `_fetch_objects`) go away — the
asyncio variant is clean.

### 5. Cover art (`mpdris2/cover.py`)

Port the 4 steps from current `find_cover()` (`src/mpDris2.in.py:689-782`)
to async:

1. `await client.readpicture(file)` / `await client.albumart(file)`
2. mutagen embedded — CPU-bound, fine to call inline (small files)
3. filesystem regex search in song dir
4. `~/.covers/{artist}-{album}.jpg` template cache

Plus the CUE/cdda fallback (`_find_alt_path`, `_try_mpd_dir_art` at
`src/mpDris2.in.py:845-899`) — same logic, async.

Returns `'file://...'` or None.

### 6. Notifications (`mpdris2/notify.py`)

Replace `gi.repository.Notify`. A small dbus-fast client that calls
`org.freedesktop.Notifications.Notify(...)` directly on the session bus.
~40 lines. Same external surface as the current
`NotifyWrapper.notify(identity, summary, icon_path)`
(`src/mpDris2.in.py:1119+`), called on playback-status changes.

### 7. Drop mmkeys

Delete `setup_mediakeys`, `register_mediakeys`,
`gsd_name_owner_changed_callback`, `mediakey_callback`
(`src/mpDris2.in.py:1005-1037` + the handler). Drop `mmkeys` from
config defaults (line 72) and from `params` parsing (line 1649).

### 8. i18n

- Keep `po/fr.po`, `po/nl.po`.
- Drop `po/POTFILES.in` (intltool format), `po/Makefile.in.in`.
- New `babel.cfg`: `[python: mpdris2/**.py]`.
- Makefile targets:
  - `i18n-extract`: `pybabel extract -F babel.cfg -o po/mpdris2.pot mpdris2/`
  - `i18n-compile`: `pybabel compile -d mpdris2/locale -D mpdris2 -i po/*.po`
- Runtime: `gettext.translation('mpdris2', localedir=<pkg dir>/locale, fallback=True).install()`
  near startup in `cli.py`.

### 9. systemd units

Two `.service` files, in `data/system/` and `data/user/`, mirroring
snapclientmpris. **Preserve** from `src/mpDris2.service.in`:
- `ConditionUser=!root` + `ConditionUser=!@system` (commit `4feeaed`)
- `Restart=always` + `RestartSec=5` (commit `c67ad7a`) — keep `always`,
  not `on-failure`
- `BindsTo=mpd.service`, `After=`/`Wants=`
- `BusName=org.mpris.MediaPlayer2.mpd`
- `ExecStart=/usr/bin/mpDris2 --use-journal --no-reconnect` (absolute
  path now; no `@bindir@` sed pass)

### 10. D-Bus activation file

`data/dbus-1/org.mpris.MediaPlayer2.mpd.service`:
```
[D-BUS Service]
Name=org.mpris.MediaPlayer2.mpd
Exec=/usr/bin/mpDris2 --use-journal
SystemdService=mpDris2.service
```

### 11. Debian packaging

`debian/control` build-deps (model: `snapclientmpris/debian/control`):
```
debhelper-compat (= 13),
dh-python,
pybuild-plugin-pyproject,
python3,
python3-setuptools,
python3-babel,
```
Runtime depends:
```
python3,
python3-mpd,
python3-dbus-fast,
python3-mutagen,
${misc:Depends}, ${python3:Depends}
```
`debian/rules`:
```make
#!/usr/bin/make -f
export PYBUILD_NAME=mpdris2

%:
	dh $@ --with python3 --buildsystem=pybuild

override_dh_auto_build:
	pybabel compile -d mpdris2/locale -D mpdris2 -i po/*.po
	dh_auto_build
```
`debian/mpdris2.install`:
```
data/user/mpDris2.service usr/lib/systemd/user/
data/system/mpDris2.service usr/lib/systemd/system/
data/dbus-1/org.mpris.MediaPlayer2.mpd.service usr/share/dbus-1/services/
data/mpdris2.desktop etc/xdg/autostart/
data/mpdris2.desktop usr/share/applications/
data/mpdris2.conf usr/share/doc/mpdris2/
```
(`.desktop` autostart kept for non-systemd desktops; systemd users get
auto-start via `default.target`.)

### 12. CI (`.github/workflows/build.yml`)

Rebuild on `snapclientmpris`'s pattern (lines 1-105):
- `lint` job: `pip install -e .[dev]`, `make check-tag` on tag pushes,
  `make lint-ruff`, `make lint-mypy`, `make test`.
- `deb` job in a `debian:trixie` container, install build deps,
  `make sync-deb` on tag, `make deb`, upload artifact.
- `release` job: `softprops/action-gh-release@v3`, prerelease detection
  on `-rc`/`-beta`/`-alpha`.
- `notify-apt-repo` job: preserve the existing
  `peter-evans/repository-dispatch@v3` to `b0bbywan/odio-apt-repo`
  (`APT_REPO_TOKEN` secret + `release-published` event-type unchanged).

### 13. Makefile

Mirror `snapclientmpris/Makefile`: `version`, `deb-version`,
`check-tag`, `sync-deb`, `lint`, `lint-ruff`, `lint-mypy`, `test`,
`build`, `deb`, `clean`, plus `i18n-extract` and `i18n-compile`.

### 14. `shell.nix`

```nix
with import <nixpkgs> {};
mkShell {
  buildInputs = [
    (python311.withPackages (ps: with ps; [
      mpd2 dbus-fast mutagen babel
      pytest pytest-asyncio mypy ruff
    ]))
  ];
}
```

### 15. `CLAUDE.md`

Rewrite the **Build System** section: `pip install -e .` (or
`nix-shell`); `make test` / `make lint` / `make build` / `make deb`;
version source of truth `mpdris2/__init__.py`; `make i18n-extract` /
`make i18n-compile`. Drop the Autotools section. Update **Source
Structure** to list each new module and its responsibility. Update
**Runtime Dependencies** for the new stack.

## Behaviors preserved (from recent commits)

| Commit | Behavior | Location after refactor |
|--------|----------|------------------------|
| `c67ad7a` | Always restart, 5s backoff | `data/{system,user}/mpDris2.service` |
| `e62fe21` | Debian packaging + APT repo notify | `debian/`, `.github/workflows/build.yml` |
| `3ee40d2` | GitHub Actions CI | `.github/workflows/build.yml` |
| `4feeaed` | `ConditionUser=!root` + `!@system` | `data/system/mpDris2.service` |
| `ccc4fab` | `readpicture`/`albumart` cover + CUE fallback | `mpdris2/cover.py` (async, same logic) |

## Execution sequencing

Switching D-Bus library + concurrency model + module layout together is
mostly all-or-nothing, but the work can ship as a series of PRs on a
`refactor/asyncio` branch:

1. **PR 1 — skeleton**: `pyproject.toml`, `mpdris2/__init__.py`,
   `__main__.py`, `cli.py` stub (argparse + config load +
   `asyncio.run` prints "hello"), `tests/test_cli.py`. Old autotools
   files kept temporarily. CI rewritten. Verify `pip install -e .`.
2. **PR 2 — D-Bus + MPD**: port `mpris.py` and `mpd_client.py`, glue
   them in `cli.py`'s `run()` coroutine. Daemon advertises on D-Bus;
   no metadata yet. Verify `playerctl status` works.
3. **PR 3 — metadata + cover + notify**: port `translate.py`,
   `cover.py`, `notify.py`. Feature parity with the old daemon (minus
   mmkeys).
4. **PR 4 — packaging + i18n + cleanup**: rewrite `debian/`, add
   `data/{system,user}/`, compile `.mo` files; delete autotools files.
   This PR is the cutover point — after merge the old build path is
   gone.
5. **PR 5 — MusicBrainz fallback** (the original ask): a 5th step in
   `cover.py`, async-native. Supersedes the old threaded design from
   `docs/musicbrainz-cover-fallback.md`.

## Follow-up: MusicBrainz fallback on the new architecture

After the refactor lands, the originally-planned cover fallback
simplifies a lot:
```python
async def musicbrainz_fallback(artist, album, cache):
    if (artist, album) in cache:
        return cache[(artist, album)] or None
    path = await asyncio.to_thread(_blocking_mb_lookup, artist, album, ...)
    cache[(artist, album)] = path or ''
    return path
```
No threading, no `GLib.idle_add`, no generation token: the call awaits
in-line; if the track changes mid-flight the result is just cached for
the next play. The on-disk write into
`~/.covers/{artist}-{album}.jpg` still integrates with step 4 of the
pipeline unchanged.

## Verification

End-to-end checks once PR 4 lands:
1. `pip install -e .` succeeds on Python 3.11.
2. `make lint test` passes (ruff + mypy + pytest).
3. `make deb` in a Debian trixie container produces
   `mpdris2_X.Y.Z_all.deb`.
4. Install the `.deb`, `systemctl --user start mpDris2`,
   `playerctl --player=mpd status` returns `Playing` and
   `playerctl metadata` shows `xesam:title/artist/album` +
   `mpris:artUrl`.
5. Cover art reaches gnome-music / KDE plasma / playerctl with
   embedded art, then filesystem-side art, then the
   `downloaded_covers` cache.
6. Stop MPD, observe reconnect-with-backoff; kill the daemon,
   observe `Restart=always RestartSec=5`.
7. French/Dutch notifications display when `LANG=fr_FR.UTF-8`.
8. `playerctl play-pause` → MPD pauses; setting `Volume` via
   `dbus-send` → MPD volume updates.
9. CI: tag `v1.0.0-rc1` on a test fork; confirm `.deb` artifact +
   GitHub release + `odio-apt-repo` dispatch.

## Files preserved as-is

`LICENSE`, `COPYING`, `README.md`, `README`, `NEWS`, `AUTHORS`,
`po/fr.po`, `po/nl.po`, `debian/changelog`, `debian/copyright`,
`debian/source/format`, `shell.nix` (updated deps), `CLAUDE.md`
(sections rewritten), `.gitignore` (extended for `build/`, `dist/`,
`__pycache__/`).
