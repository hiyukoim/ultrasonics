# Agent Handoff — Ultrasonics

## Project Goal

Turn ultrasonics into a self-hosted Soundiiz alternative: sync playlists between Spotify, Navidrome, local files, and CSV — with lossless round-trip export, fuzzy matching, and persistent tracking of unmatched songs.

## What Has Been Implemented

| File | Role |
|------|------|
| `ultrasonics/official_plugins/up_csv.py` | CSV import + export (both directions). Lossless canonical schema, exportify/Soundiiz header-alias mapping. |
| `ultrasonics/official_plugins/up_navidrome.py` | Navidrome input/output with **UnmatchedStore** (sqlite, `config_dir/up_navidrome/unmatched.db`). Retries previously-failed songs each run. |
| `ultrasonics/official_plugins/up_isrc matcher.py` | Slimmed dedup modifier — delegates scoring to `fuzzymatch.similarity()`, keeps only metadata-merge. |
| `ultrasonics/official_plugins/up_spotify public.py` | Read-only input for public/user Spotify playlists (dev-mode API). |
| `docs/specs/` | Soundiiz backend spec, platform adapters spec, API spec. |

## Key Conventions

- **Plugin file** = `up_<name>.py`. `handshake["name"]` must equal `<name>`.
- **Plugin API**: `handshake` dict, `run(settings_dict, **kwargs)`, `builder(**kwargs)`, optional `test(database, **kwargs)`.
- `kwargs` always contains: `database`, `global_settings`, `component`, `applet_id`, `songs_dict`.
- **Song dict shape**: `{title, artists:[], album, date, isrc, location, id:{provider: value}}`.
- **songs_dict shape**: `[{name, id:{}, songs:[song, ...]}]`.
- Matching hierarchy (in `fuzzymatch.similarity`): location > service IDs > ISRC > fuzzy title/artist/album. Do not add a parallel matcher.
- CSV artists delimiter = `; ` (semicolon-space). IDs = `provider:value|provider:value`.
- Python/Flask only. No npm. No new pip deps without explicit approval.
- `package-lock.json` is gitignored — this is not a Node project.

## Limitations / Next Steps

- `up_spotify public` cannot access Spotify editorial/official playlists (dev-mode 404). Use exportify CSV import for those.
- No auth/UI for the unmatched-track store yet — songs accumulate in sqlite but there's no web view to manually resolve them.
- No scheduler integration tests — only unit-level verification exists for `up_csv` and `up_isrc matcher`.
- `up_navidrome` output: playlist creation via Navidrome API is not implemented (only song matching into existing playlists).

## Gotchas

- Plugin filenames have **spaces** (e.g. `up_custom file.py`). Python imports them via `importlib`; never rename without updating `plugins.py` discovery.
- `fuzzymatch` depends on `fuzzywuzzy` (in `requirements.txt`). It is the single source of truth for song similarity scoring.
- `up_csv` import decides artist-split strategy by **matched header alias**, not cell content. `"artist name(s)"` = exportify comma-split; anything else = semicolon-split.
- The webapp uses Jinja2 templates in `ultrasonics/templates/`. Plugin builder dicts render as form fields — follow the `type`/`label`/`name`/`value` schema exactly.
