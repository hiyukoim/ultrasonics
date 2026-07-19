# Autonomous Agent Work Plan — Ultrasonics → Soundiiz parity

This is a personal fork: a self-hosted Soundiiz alternative and **source of truth** for playlists across Spotify, Navidrome, SoundCloud, Bandcamp. The app must keep track info even when a track isn't findable on a platform (orphans), and allow relinking later. Build order now: finish **T8**, then **T0 (vault)**, migrate unmatched into it, then adapter waves.

Work through tasks **top to bottom, one commit per task**. Do not wait for approval between tasks. Only stop to ask if a task is truly blocked (missing credential, external API 404, ambiguous product decision). Otherwise pick the reasonable option and keep going.

## Global rules (apply to every task)
- Python 3 / Flask only. **No JS frameworks, no npm, no build tooling.** Never add `package-lock.json`.
- Match existing plugin conventions: `handshake` / `run(settings_dict, **kwargs)` dispatch on `component` / `builder(**kwargs)` / `test(database, **kwargs)`. Song dict = `{title, artists[], album, date, isrc, location, id:{provider:val}}`. Playlist = `{name, id, songs[]}`.
- Reuse `ultrasonics/tools/` (`fuzzymatch.similarity` already does ISRC→id→location→title/artist/album; `name_filter`). Do not reimplement matching.
- Per-plugin state = sqlite in `_ultrasonics["config_dir"]/<plugin>/`. No new deps beyond `requirements.txt` unless a task says so.
- LLM features: use free providers (Claude/Gemini via user's subs), never a paid API key.
- Each task: implement → add/run a self-test proving the acceptance criteria → commit with the given message → move on.
- Report format when done with a batch: one line per task (done / blocked+why). Don't paste full diffs.

## Definition of done (per task)
Code follows conventions · acceptance test passes · committed · no stray artifacts · docstrings accurate (no claims the code doesn't do).

---

## TASK LIST

### T0 — Canonical library "vault" (source-of-truth core) — DO BEFORE ADAPTER WAVES
Makes the app a persistent hub, not a stateless pipe. Orphaned tracks are kept, never dropped.
- New `ultrasonics/tools/vault.py` (sqlite in `config_dir/vault/vault.db`):
  ```
  tracks(canonical_id PK, isrc, upc, title, artists, album, date, image, created, updated)
  track_links(canonical_id, platform, platform_id, status)   -- status: linked | orphan  · UNIQUE(canonical_id, platform)
  playlists(canonical_id PK, name, description, image, updated)
  playlist_tracks(playlist_canonical_id, track_canonical_id, position)
  ```
- API: `upsert_track(song) -> canonical_id` (resolve existing by ISRC → any platform_id → fuzzy via `tools/fuzzymatch`, else create); `link(canonical_id, platform, platform_id, status)`; `mark_orphan(canonical_id, platform)`; `get_orphans(platform)`; `upsert_playlist(...)`, `set_membership(...)`.
- **Adapter contract change:** syncs go **platform → vault → platform**, not adapter→adapter. On import: upsert each song into vault + link its source platform. On export: for each vault track, use its `linked` id for the target platform; if none, run resolution; still nothing → write an `orphan` link for that platform (keep the track).
- **Migrate** the per-plugin `unmatched` store into `track_links` where `status='orphan'`. The T2 manual-match UI and T1 `matchings` now read/write the vault.
- Accept: import a Spotify + a Bandcamp playlist → both land in one vault; a Bandcamp-only track with no Spotify match persists as a `tracks` row with a Spotify `orphan` link (not dropped); re-running export after the track becomes findable flips orphan→linked.

### T1 — `matchings` store + auto-apply (learning loop)
- New `ultrasonics/tools/matchings.py`: sqlite `matchings(user_key, src_platform, src_id, src_isrc, dst_platform, dst_id, created)`. API: `save(...)`, `lookup(src_platform, src_id, dst_platform)`, `list_unresolved(applet_id)`.
- Wire into `up_navidrome` output: before search, check `matchings.lookup`; on manual resolve, `save`.
- Accept: a saved src→dst pair is auto-applied on next run without re-searching. Unit test proves lookup hit.

### T2 — Manual-matching web view
- Flask route + minimal HTML (server-rendered, no framework) listing `unmatched` rows per applet; per row: a `search()` box hitting the destination adapter, pick a result → writes to `matchings` (T1) and removes from `unmatched`.
- Accept: unresolved track can be resolved in UI; next applet run picks it up.

### T3 — Run-now trigger
- Endpoint + button to enqueue an applet immediately (Soundiiz empty-POST equivalent). 
- Accept: clicking runs the applet now, logs a history row (T4).

### T4 — Run history
- `ultrasonics/tools/history.py`: sqlite `runs(applet_id, start, end, statut, n_playlists, n_tracks, n_unmatched, error)`. Write one row per applet execution. Flask view listing recent runs.
- Accept: each run appears with counts; errors captured.

### T5 — Notifications
- Emit on run end / error / platform-disconnected. Channels: reuse `up_webhook`; add optional email (stdlib `smtplib`, SMTP settings in global config).
- Accept: a failing run triggers a TASK_ERROR notification via configured channel.

### T6 — Remove-duplicates modifier
- `up_dedupe` modifier: dedupe within each playlist using `fuzzymatch.similarity` ≥ threshold; merge metadata into survivor. (Do not duplicate `up_isrc matcher`; if it already covers this, extend it instead of adding a new plugin — decide and note which.)
- Accept: playlist with known dupes collapses correctly; test with ISRC-equal + fuzzy-title cases.

### T7 — URL import input
- `up_url` input: accept any public playlist URL, resolve to `songs_dict`. Start with Spotify/Deezer/Apple public URLs via their public endpoints; unknown hosts → clear error.
- Accept: a public Deezer/Spotify playlist URL imports to songs_dict.

### T8 — Favorites / albums / artists sync modes
- Extend `up_navidrome` (and `up_spotify`, `up_deezer` where the API allows) to handle `mode` beyond playlists: favorites (tracks), albums (key=UPC), artists (key=name+genres).
- Accept: favorites round-trip between two configured adapters; album match uses UPC when present.

### T9 — Adapter wave 1 (shared Subsonic base + hi-fi)
- Refactor a `SubsonicBase` mixin from `up_navidrome`; add `up_subsonic`, `up_jellyfin`, `up_emby` on it. Add `up_tidal`, `up_qobuz` (auth pattern D, see adapters-spec).
- Accept: each new adapter passes its `test()` against a configured server/account (or is skipped-with-note if no creds).

### T10 — Adapter wave 2
- `up_ytmusic` (unofficial, ytmusicapi-style), `up_youtube` (Data API v3), `up_amazonmusic`, `up_soundcloud`. Mark ⚠️ ones clearly; isolate failures (one adapter breaking must not break others).
- Accept: at least read-path works per adapter with creds; write-path where API allows.

### T11 — AI playlist generator input
- `up_ai generator` input: settings genre/mood/decade/count → free LLM (Claude/Gemini sub) → list of `{title, artists[]}` (platform/id null) → return songs_dict for the matching pipeline to resolve downstream.
- Accept: a request yields N title+artist items; feeding into an output adapter resolves them.

### T12 — Quota + adapter capability guards
- Enforce `maxTracksInPlaylist` per adapter (truncate/split + warn) and validate input/output selection against each adapter's read/write capability (from adapters-spec matrix) before running.
- Accept: oversized playlist is handled per adapter; invalid src/dst combo is rejected with a clear message.

### (Optional, only if going multi-tenant) T13 — accounts/quota/billing
- User accounts, session+CSRF, per-user plugin config, Stripe. Skip unless self-host-for-others is the goal.

---

Reference specs (this folder): `soundiiz-api-spec-for-bolt.md` (endpoints/schemas), `soundiiz-backend-implementation-spec.md` (internals/DB/engine), `soundiiz-platform-adapters-spec.md` (66 adapters, auth patterns, capability matrix). Consult them per task instead of guessing.