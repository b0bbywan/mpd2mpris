# MusicBrainz / Cover Art Archive fallback for missing covers

> **Deferred until after the asyncio/dbus-fast refactor.** This document
> targets the *current* (autotools + sync + dbus-python + GLib) stack
> and would be substantially rewritten for the post-refactor codebase.
>
> See [`./refactor-asyncio-dbus-fast.md`](./refactor-asyncio-dbus-fast.md)
> for the migration; the MusicBrainz feature lands as PR 5 in that
> sequence, with a much simpler `asyncio.to_thread` design (no threads,
> no `GLib.idle_add`, no generation token). The on-disk cache strategy
> below (write into the `downloaded_covers` template at
> `~/.covers/{artist}-{album}.jpg` so step 4 picks it up unchanged) is
> the only part that carries over verbatim.

---

## Context (pre-refactor)

Today `find_cover()` (`src/mpDris2.in.py:689-782`) walks 4 local sources:
MPD `readpicture`/`albumart`, mutagen embedded art, filesystem regex, then
the local cache `~/.covers/{artist}-{album}.jpg` via the `downloaded_covers`
template (line 232). When all four miss, `mpris:artUrl` stays empty —
typical case is a remote MPD whose files are not on the local FS and whose
tracks have no embedded art.

The snapcast `meta_mpd.py` plugin queries MusicBrainz then Cover Art Archive
as a last resort. We reuse that idea but adapt it to a GLib daemon (no
mainloop blocking) and to the existing mpDris2 pipeline: the result is
**written into the `downloaded_covers` disk cache** that step 4 already
reads, so any future play of that album (or by any other tool) finds the
image locally with no further network call.

User-confirmed choices:
- Async + on-disk cache (mainloop never blocks).
- Opt-in (`musicbrainz_fallback = false` by default; user enables it in
  config).
- 500px ("large") thumbnail.

## Approach (pre-refactor — supersede after refactor)

1. Add `musicbrainzngs` as an **optional** dependency, mirroring the
   existing `mutagen` pattern (lines 42-45).
2. Plug a 5th step into `find_cover()`: when artist+album are known and the
   option is enabled, kick off the lookup in a daemon thread, then deliver
   the result back to the mainloop via `GLib.idle_add` → mutate
   `self._metadata['mpris:artUrl']` → emit `PropertiesChanged` through the
   existing `update_property` helper (`src/mpDris2.in.py:1362`).
3. Persist the downloaded image into `~/.covers/{artist}-{album}.jpg`
   atomically (tmp file + rename) so it slots into the step-4 cache.
4. Keep an in-memory miss sentinel (`''`) keyed by `(artist, album)` to
   avoid re-hitting MusicBrainz on replays of an unmatched album.
5. Bump a generation counter on every `update_metadata()` so a result
   arriving after a track change is discarded.

(Detailed diff omitted — to be redesigned on the new architecture.)
