# mpDris2

mpDris2 provide MPRIS 2 support to mpd (Music Player Daemon).

mpDris2 is run in the user session and monitors a local or distant mpd server.

# Install

From the Odio APT repository:

```sh
curl -fsSL https://apt.odio.love/key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/odio.gpg
echo "deb [signed-by=/usr/share/keyrings/odio.gpg] https://apt.odio.love stable main" \
    | sudo tee /etc/apt/sources.list.d/odio.list
sudo apt update
sudo apt install mpdris2
```

Enable for your user session (mpdris2 talks to the session bus, not the
system bus):

```sh
systemctl --user enable --now mpDris2.service
```

## From git

```sh
git clone https://github.com/b0bbywan/mpDris2.git
cd mpDris2
pipx install .          # or: pip install --user .
```

This installs the `mpDris2` console script into your `$PATH`. Start it
from your desktop's autostart, or via a `systemctl --user` unit.

Runtime dependencies: `python-mpd2` and `dbus-fast`. Python 3.11+.

# Configuration

By default mpDris2 connects to `localhost:6600`. Environment variables
`$MPD_HOST` and `$MPD_PORT` are honoured. To change anything else, copy
the example file shipped at `/usr/share/doc/mpdris2/mpDris2.conf` to
`~/.config/mpDris2/mpDris2.conf` and edit.

Cover-art resolution needs `music_dir` to be set (or auto-detected over
a local Unix socket connection to MPD). See [Cover art](#cover-art)
below for the full pipeline.

Restart mpDris2 (`pkill -HUP mpDris2`, or just restart your session) to
pick up config changes.

> **Note:** the `[Bling] mmkeys` option from the historical mpDris2 is
> no longer supported. Modern desktops (GNOME, KDE, sway with
> `mpris-ctrl`, …) read MPRIS directly for multimedia-key handling, so
> mpDris2 doesn't need to grab the keys itself anymore.

## Sample configuration

```ini
[Connection]
# Override host / port (or set $MPD_HOST / $MPD_PORT in the environment).
host = 192.168.1.5
port = 6600
password =

[Library]
# Required for cover-art resolution when MPD is remote (auto-detected
# over a local Unix socket connection).
music_dir = /media/music/
# Override the default cover-file regex; useful for non-standard names.
#cover_regex = ^(album|cover|\.?folder|front).*\.(gif|jpe?g|png|webp|bmp)$
# Where the downloaded-covers cache lives (defaults to $XDG_CACHE_HOME/mpDris2/).
#cover_cache_dir =

[Bling]
# Send desktop notifications on track change.
notify = True
# Also notify when the player is paused (default: only when playing).
notify_paused = False
# CD-like Previous: if elapsed >= 3 s, restart the current track instead
# of jumping to the previous one.
cdprev = False

[Notify]
# Urgency: 0 low, 1 normal, 2 critical.
urgency = 1
# Bubble lifetime in ms — -1 lets the notification server decide.
timeout = -1
# Templates for the bubble. Empty = built-in default.
# Placeholders: %album% %title% %id% %time% %timeposition% %date% %track%
#               %disc% %artist% %albumartist% %composer% %genre% %file%
summary =
body =
paused_summary =
paused_body =
```

With `notify = True`, mpDris2 also raises a brief bubble when playback
stops, and when the MPD connection drops or comes back.

# Architecture

This branch is the asyncio + dbus-fast rewrite of the original
PyGObject / dbus-python implementation: a single asyncio event loop
replaces the GLib MainLoop + thread pool, `dbus-fast` replaces
`dbus-python`, and `mpd.asyncio` from `python-mpd2` replaces the
blocking client. See
[`docs/refactor-asyncio-dbus-fast.md`](docs/refactor-asyncio-dbus-fast.md)
for the design notes.

# Development

A top-level `Makefile` wraps the day-to-day commands so local dev and
CI stay in sync (the GitHub workflow calls the same targets):

```sh
make lint           # ruff + mypy
make test           # pytest
make build          # python -m build (sdist + wheel)
make deb            # dpkg-buildpackage -b -us -uc (Debian toolchain)
make clean          # drop build/, dist/, *.egg-info
make version        # print the Python version (from __init__.py)
make sync-deb       # bump debian/changelog to match __init__.py
make i18n-extract   # refresh po/mpdris2.pot from source
make i18n-compile   # compile po/*.po into the runtime locale tree
```

`mpdris2/__init__.py` is the single source of truth for the version;
`make sync-deb` and `make check-tag TAG=…` keep `debian/changelog`
and the git tag aligned with it.

# Build a .deb

Build-deps (per `debian/control`): `debhelper-compat (= 13)`,
`dh-python`, `python3`, `python3-setuptools`. Then `make deb` on
Debian trixie or a derivative produces the `.deb`. The runtime deps
(`python3-mpd`, `python3-dbus-fast`) are resolved by APT at install
time, not at build time.

# Cover art

mpDris2 resolves `mpris:artUrl` through a fixed pipeline. The first
step that yields a usable image wins; later steps are skipped.

| # | Step | Source | Exposed `mpris:artUrl` | Wins when… |
|---|------|--------|------------------------|-----------|
| 1 | MPD `readpicture` | Embedded picture in the audio file (FLAC `PICTURE`, ID3 `APIC`, …) | `file:///tmp/cover-*.{jpg,png,…}` | The track carries embedded art |
| 2 | FS regex scan | `cover_regex` match in the song's directory (default matches `cover.*`, `folder.*`, `album.*`, `front.*`) | `file://` URI of the matched file (RFC-3986 percent-encoded) | A non-standardly-named cover sits next to the audio file (local FS only) |
| 3 | MPD `albumart` | `cover.{png,jpg,jxl,webp}` in the song's directory (resolved server-side by MPD) | `file:///tmp/cover-*.{jpg,png,…}` | Remote MPD, or step 2 missed (standard name only) |
| 4 | CUE/cdda fallback | `cover_regex` match next to the loaded `.cue` playlist (FS scan), falling back to MPD `albumart` (which server-side resolves `cover.{png,jpg,jxl,webp}`) when music_dir isn't locally accessible | `file://` URI of the matched file (local FS) or `file:///tmp/cover-*` (remote MPD) | The song is a CUE virtual track (cdda://, http://, …) and the CUE's own directory holds a cover |
| 5 | XDG cover cache | `$XDG_CACHE_HOME/mpDris2/{artist}-{album}.{jpg,png,…}` | `file://` URI of the cached file | Earlier steps failed and a previous run (or the optional MusicBrainz fallback) populated the cache |
