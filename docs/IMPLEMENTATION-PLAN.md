# Ultrasonics → Soundiiz-parity Implementation Plan

Source of truth: the three extracted specs in this folder (HAR-derived). This plan maps **every Soundiiz feature** onto ultrasonics' plugin model (inputs / modifiers / outputs / triggers + core). Goal: add all features.

Legend: ✅ done · 🟡 partial · ⬜ todo · ⛔ blocked

---

## Status of what's built (branch `test`)
| Item | Ultrasonics form | Status |
|---|---|---|
| Navidrome/Subsonic sync | `up_navidrome` (input+output) | ✅ |
| ISRC-first matching | core `tools/fuzzymatch` + `up_isrc matcher` (slimmed) | ✅ |
| CSV import/export | `up_csv` (input+output) | ✅ |
| Spotify public (non-editorial) | `up_spotify public` (input) | ✅ |
| Unmatched-track store | sqlite in `up_navidrome` | ✅ |
| Editorial/official Spotify playlists | exportify → `up_csv` | ✅ (workflow) |

Upstream already ships: `up_spotify`, `up_deezer`, `up_lastfm`, `up_plex`, `up_local music database`, `up_local playlists`, `up_playlist merger`, `up_spotify mixer`, `up_time trigger`, `up_webhook`, `up_log tracks`, `up_custom file`.

---

## Feature → work-item map (Soundiiz surface, from api-spec)

### Phase 1 — Transfer core (mostly done)
- ✅ Playlist convert (input→output) = ultrasonics applet
- ✅ Track matching (ISRC/UPC→fuzzy)
- ✅ Unmatched persistence + retry-on-next-run
- ⬜ **Manual matching UI** — surface `unmatched` rows in web UI; `tracks/search` per platform → user picks → write to a `matchings` store → auto-apply next run (Soundiiz's learning loop). *Core + per-adapter `search()`.*

### Phase 2 — Library sync beyond playlists
Soundiiz syncs tracks/albums/artists, not just playlists.
- 🟡 Favorites (tracks) — some adapters; standardize a `favorites` mode across adapters
- ⬜ Albums sync (key = UPC)
- ⬜ Artists sync (key = name+genres)
- ⬜ Add `mode: ["songs","playlists","favorites","albums","artists"]` handling to adapters

### Phase 3 — Automation & history
- ✅ Scheduled auto-sync = `up_time trigger`
- ⬜ **Run-now trigger** (Soundiiz empty-POST) — manual "sync now" button per applet
- ⬜ **Batch/history view** — persist run results (counts playlists/tracks, start/end, statut, errors) + web view; ultrasonics has partial run logs, formalize into a `history` table
- ⬜ **Notifications** — BATCH_END / TASK_ERROR / PLATFORM_DISCONNECTED (email/webhook); `up_webhook` covers webhook path

### Phase 4 — Playlist management ops
- ⬜ Remove-duplicates modifier (dedup within/across playlists)
- ⬜ Delete playlists (per-adapter)
- ⬜ Playlist image set (where writable; not Subsonic)
- ✅ Merge playlists = `up_playlist merger`

### Phase 5 — Ingest sources
- ✅ CSV import (`up_csv`)
- ⬜ **URL import** modifier/input (`platform:"url"`) — accept any public playlist URL
- ⬜ **AI playlist generator** input — genre/mood/decade → track list (title+artist only) → matching pipeline resolves to platform. Use free LLM (per user pref: Claude/Gemini subs, not paid API).

### Phase 6 — Platform adapters (66 total; see adapters-spec)
Build by auth pattern, first-wave priority. Existing: spotify, deezer, lastfm, plex, navidrome.
- ⬜ Wave 1: tidal, qobuz, apple music, subsonic/jellyfin/emby (Subsonic-family = shared base)
- ⬜ Wave 2: ytmusic, youtube, amazonmusic, soundcloud
- ⬜ Wave 3: napster, audiomack, audius, beatport, yandex, jiosaavn
- ⬜ Skip: defunct + telco/B2B long-tail (adapters-spec Tier 5/6)

### Phase 7 — Multi-user / accounts (only if hosting for others)
- ⬜ User accounts, session+CSRF, per-user plugin config
- ⬜ Quota (max scheduled tasks, maxTracksInPlaylist per adapter)
- ⬜ Billing (Stripe) — only if commercial

---

## Build order (recommended)
1. Manual matching UI + `matchings` store (Phase 1) — highest UX value, completes the transfer loop.
2. Run-now + history + notifications (Phase 3).
3. Remove-duplicates + URL import (Phase 4/5).
4. Favorites/albums/artists modes (Phase 2).
5. Adapter waves 1→3 (Phase 6) — pure additions once core is stable.
6. AI generator (Phase 5).
7. Multi-user/quota/billing (Phase 7) — only if going multi-tenant.

Constraint throughout: Python/Flask, lightweight, existing plugin conventions, no JS frameworks, free LLM providers only.
