#!/usr/bin/env python3

"""
up_navidrome

Input and output plugin for Navidrome (and any Subsonic-compatible server).
Supports playlists, favorites (starred tracks), albums, and artists.

Uses the Subsonic REST API with token-based authentication.
Auth and CRUD methods shared via SubsonicBase.
"""

from tqdm import tqdm

from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter
from ultrasonics.tools.subsonic_base import SubsonicBase

log = logs.create_log(__name__)

handshake = {
    "name": "navidrome",
    "description": "sync playlists, favorites, albums, and artists to/from a navidrome or subsonic server",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.3",
    "settings": [
        {"type": "text",   "label": "Server URL", "name": "server_url", "value": "e.g. http://localhost:4533"},
        {"type": "text",   "label": "Username",   "name": "username",   "value": ""},
        {"type": "text",   "label": "Password",   "name": "password",   "value": ""},
        {"type": "select", "label": "Sync Mode",  "name": "sync_mode",
         "options": ["playlists", "favorites", "albums", "artists"], "value": "playlists"},
        {"type": "string", "value": "Fuzzy ratio: 100 = exact match, 0 = anything. Recommended: 85."},
        {"type": "text",   "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": "85"},
    ],
}


class _NavidromeAPI(SubsonicBase):
    ID_KEY = "navidrome"

    def __init__(self, server_url, username, password):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password


def run(settings_dict, **kwargs):
    database      = kwargs["database"]
    component     = kwargs["component"]
    applet_id     = kwargs["applet_id"]
    songs_dict    = kwargs["songs_dict"]

    server_url = database.get("server_url", "").strip()
    username   = database.get("username",   "").strip()
    password   = database.get("password",   "")

    if not all([server_url, username, password]):
        raise Exception("Navidrome: server URL, username, and password are required.")

    api = _NavidromeAPI(server_url, username, password)

    sync_mode   = database.get("sync_mode", "playlists")
    fuzzy_ratio = _parse_ratio(settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio"))

    # ── FAVORITES ─────────────────────────────────────────────────────────────

    if sync_mode == "favorites":
        if component == "inputs":
            return [{"name": "Favorites",
                     "id": {api.ID_KEY: "__favorites__"},
                     "songs": [api.track_to_songs_dict(t) for t in api.get_starred()]}]

        existing_ids = {t.get("id") for t in api.get_starred()}
        for pl in songs_dict:
            for song in pl.get("songs", []):
                mid = api.resolve_track(song, fuzzy_ratio, matchings)
                if mid and mid not in existing_ids:
                    try:
                        api.star(mid)
                        existing_ids.add(mid)
                    except Exception as e:
                        log.warning(f"navidrome star failed: {e}")

    # ── ALBUMS ────────────────────────────────────────────────────────────────

    elif sync_mode == "albums":
        if component == "inputs":
            all_albums, offset = [], 0
            while True:
                batch = api.get_album_list(size=500, offset=offset)
                if not batch:
                    break
                all_albums.extend(batch)
                offset += len(batch)
                if len(batch) < 500:
                    break
            return [{"name": "Albums",
                     "id": {api.ID_KEY: "__albums__"},
                     "songs": [api.album_to_dict(a) for a in all_albums]}]

        for pl in songs_dict:
            for alb in pl.get("songs", []):
                q = " ".join(filter(None, [(alb.get("artists") or [""])[0], alb.get("name", "")]))
                results = api.search_albums(q, count=10)
                best_score, best_match = 0, None
                for r in results:
                    r_dict = api.album_to_dict(r)
                    if alb.get("mbid") and r.get("musicBrainzId") == alb["mbid"]:
                        best_match, best_score = r, 101
                        break
                    score = fuzzymatch.similarity(alb, r_dict)
                    if score and score > best_score:
                        best_score, best_match = score, r
                if best_match and best_score >= fuzzy_ratio:
                    log.info(f"Album '{alb.get('name')}' matched (score={best_score:.0f})")
                else:
                    log.debug(f"Album '{alb.get('name')}' not found on Navidrome")

    # ── ARTISTS ───────────────────────────────────────────────────────────────

    elif sync_mode == "artists":
        if component == "inputs":
            return [{"name": "Artists",
                     "id": {api.ID_KEY: "__artists__"},
                     "songs": [api.artist_to_dict(a) for a in api.get_artists()]}]

        for pl in songs_dict:
            for art in pl.get("songs", []):
                name = art.get("name", "")
                if not name:
                    continue
                results = api.search_artists(name, count=5)
                for r in results:
                    score = fuzzymatch.similarity(
                        {"title": name, "artists": [name]},
                        {"title": r.get("name", ""), "artists": [r.get("name", "")]},
                    )
                    if score and score >= fuzzy_ratio:
                        log.info(f"Artist '{name}' matched (score={score:.0f})")
                        break

    # ── PLAYLISTS (default) ───────────────────────────────────────────────────

    else:
        if component == "inputs":
            playlists = api.get_playlists()
            result = [{"name": pl.get("name", "Untitled"),
                       "id": {api.ID_KEY: str(pl.get("id", ""))}}
                      for pl in playlists]
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching Navidrome playlists"):
                data     = api.get_playlist(pl["id"][api.ID_KEY])
                entries  = data.get("entry", [])
                if isinstance(entries, dict):
                    entries = [entries]
                result[i]["songs"] = [api.track_to_songs_dict(e) for e in entries]
            return result

        # Outputs: playlists
        existing_pls  = api.get_playlists()
        name_to_id    = {pl.get("name", ""): pl.get("id") for pl in existing_pls}

        for playlist in songs_dict:
            pl_name = playlist.get("name", "Untitled")
            pl_id   = name_to_id.get(pl_name)

            if not pl_id:
                try:
                    pl_id = api.create_playlist(pl_name)
                except Exception as e:
                    log.error(f"Could not create Navidrome playlist '{pl_name}': {e}")
                    continue

            existing_data   = api.get_playlist(pl_id)
            existing_entries = existing_data.get("entry", [])
            if isinstance(existing_entries, dict):
                existing_entries = [existing_entries]
            existing_tracks = [api.track_to_songs_dict(e) for e in existing_entries]
            existing_ids    = [e.get("id") for e in existing_entries]

            to_add = []
            for song in tqdm(playlist.get("songs", []), desc=f"Matching '{pl_name}'"):
                if fuzzymatch.duplicate(song, existing_tracks, fuzzy_ratio):
                    continue
                mid = api.resolve_track(song, fuzzy_ratio, matchings)
                if mid and str(mid) not in [str(x) for x in existing_ids]:
                    to_add.append(mid)
                elif not mid:
                    log.debug(f"No match for '{song.get('title')}' on Navidrome")

            if settings_dict.get("existing_playlists") == "Update" and existing_entries:
                remove_indices = [
                    idx for idx, e in enumerate(existing_entries)
                    if not any(
                        fuzzymatch.similarity(song, api.track_to_songs_dict(e)) >= fuzzy_ratio
                        for song in playlist.get("songs", [])
                    )
                ]
                if remove_indices:
                    api.update_playlist_remove(pl_id, remove_indices)

            if to_add:
                api.update_playlist_add(pl_id, to_add)


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_ratio(val, default=85.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def test(database, **kwargs):
    server_url = database.get("server_url", "").strip()
    username   = database.get("username",   "").strip()
    password   = database.get("password",   "")
    if not all([server_url, username, password]):
        raise Exception("Server URL, username, and password are required.")
    api = _NavidromeAPI(server_url, username, password)
    api.ping()
    log.info(f"Navidrome connected: {server_url}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch playlists/favorites/albums/artists from Navidrome."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": "Write to Navidrome."},
        {"type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
         "id": "existing_playlists", "options": ["Append", "Update"], "required": True},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
