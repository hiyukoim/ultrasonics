#!/usr/bin/env python3

"""
up_soundcloud

Input and output plugin for SoundCloud.
Auth: OAuth2 Bearer token (official API).
Max 500 tracks per playlist. Failures are isolated per-song.
Vault contract: flips orphan→linked via resolver.
"""

import time

import requests
from tqdm import tqdm

from ultrasonics import logs
from ultrasonics.tools import matchings, name_filter
from ultrasonics.tools.resolver import resolve_track

log = logs.create_log(__name__)

handshake = {
    "name": "soundcloud",
    "description": "sync playlists and favorites to/from soundcloud (max 500 tracks/playlist)",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites"],
    "version": "0.2",
    "settings": [
        {"type": "string",
         "value": "SoundCloud uses OAuth2. Paste your access token below. Playlists are capped at 500 tracks."},
        {"type": "text", "label": "Access Token",  "name": "access_token", "value": ""},
        {"type": "text", "label": "Client ID (optional)", "name": "client_id", "value": ""},
        {"type": "select", "label": "Sync Mode", "name": "sync_mode",
         "options": ["playlists", "favorites"], "value": "playlists"},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": "85"},
    ],
}

_BASE       = "https://api.soundcloud.com"
_MAX_TRACKS = 500
_ID_KEY     = "soundcloud"


def _parse_ratio(val, default=85.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class SoundCloudAPI:
    def __init__(self, access_token, client_id=None):
        self.access_token = access_token
        self.client_id    = client_id
        self._me_cache    = None

    def _headers(self):
        return {"Authorization": f"OAuth {self.access_token}",
                "Accept": "application/json; charset=utf-8"}

    def _get(self, path, params=None):
        resp = requests.get(f"{_BASE}{path}", headers=self._headers(),
                            params=params or {}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, json_body=None, params=None):
        resp = requests.post(f"{_BASE}{path}", headers=self._headers(),
                             json=json_body or {}, params=params or {}, timeout=30)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    def _put(self, path, json_body=None, params=None):
        resp = requests.put(f"{_BASE}{path}", headers=self._headers(),
                            json=json_body or {}, params=params or {}, timeout=30)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    def me(self):
        if not self._me_cache:
            self._me_cache = self._get("/me")
        return self._me_cache

    # ── playlists ─────────────────────────────────────────────────────────────

    def get_playlists(self):
        return self._get(f"/users/{self.me()['id']}/playlists")

    def get_playlist_tracks(self, playlist_id):
        data   = self._get(f"/playlists/{playlist_id}", {"representation": "compact"})
        tracks = data.get("tracks", [])
        if tracks and isinstance(tracks[0], dict) and not tracks[0].get("title"):
            ids = [str(t["id"]) for t in tracks[:_MAX_TRACKS]]
            return self._hydrate_tracks(ids)
        return tracks[:_MAX_TRACKS]

    def _hydrate_tracks(self, track_ids):
        results = []
        for i in range(0, len(track_ids), 50):
            try:
                data = self._get("/tracks", {"ids": ",".join(track_ids[i:i + 50])})
                results.extend(data if isinstance(data, list) else data.get("collection", []))
            except Exception as e:
                log.warning(f"SoundCloud hydrate failed: {e}")
        return results

    def create_playlist(self, name):
        data = self._post("/playlists", {"playlist": {"title": name, "sharing": "private"}})
        return str(data.get("id", ""))

    def update_playlist_tracks(self, playlist_id, track_ids):
        self._put(f"/playlists/{playlist_id}",
                  {"playlist": {"tracks": [{"id": int(t)} for t in track_ids[:_MAX_TRACKS]]}})

    # ── favorites ─────────────────────────────────────────────────────────────

    def get_likes(self):
        all_items, url = [], f"{_BASE}/users/{self.me()['id']}/track_likes"
        params = {"limit": 200}
        while url:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("collection", data if isinstance(data, list) else [])
            all_items.extend(items)
            url, params = (data.get("next_href") if isinstance(data, dict) else None), {}
        return all_items

    def like_track(self, track_id):
        requests.put(f"{_BASE}/users/{self.me()['id']}/track_likes/{track_id}",
                     headers=self._headers(), timeout=15).raise_for_status()

    # ── search ────────────────────────────────────────────────────────────────

    def search_tracks(self, query, limit=10):
        data = self._get("/tracks", {"q": query, "limit": limit})
        return data if isinstance(data, list) else data.get("collection", [])

    # ── conversion ────────────────────────────────────────────────────────────

    def track_to_songs_dict(self, track):
        if isinstance(track, dict) and "track" in track:
            track = track["track"]
        artist = track.get("user", {}).get("username") or track.get("user_name") or ""
        d = {"title": track.get("title", "")}
        if artist:
            d["artists"] = [artist]
        if track.get("id"):
            d["id"] = {_ID_KEY: str(track["id"])}
        return {k: v for k, v in d.items() if v}


def run(settings_dict, **kwargs):
    database   = kwargs["database"]
    component  = kwargs["component"]
    applet_id  = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    access_token = database.get("access_token", "").strip()
    if not access_token:
        raise Exception("SoundCloud access token is required.")

    api = SoundCloudAPI(access_token, database.get("client_id", "").strip() or None)
    try:
        api.me()
    except Exception as e:
        raise Exception(f"SoundCloud auth failed: {e}")

    sync_mode   = database.get("sync_mode", "playlists")
    fuzzy_ratio = _parse_ratio(settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio"))

    def _match(song):
        return resolve_track(
            song, _ID_KEY,
            lambda q: api.search_tracks(q, limit=10),
            api.track_to_songs_dict,
            fuzzy_ratio, matchings,
        )

    if sync_mode == "favorites":
        if component == "inputs":
            liked  = api.get_likes()
            tracks = [api.track_to_songs_dict(t) for t in liked]
            return [{"name": "Favorites", "id": {_ID_KEY: "__favorites__"}, "songs": tracks}]

        for playlist in songs_dict:
            for song in playlist.get("songs", []):
                try:
                    mid = _match(song)
                    if mid:
                        api.like_track(mid)
                        time.sleep(0.5)
                except Exception as e:
                    log.warning(f"SoundCloud like failed for '{song.get('title')}': {e}")

    else:
        # playlists
        if component == "inputs":
            playlists = api.get_playlists()
            result    = [{"name": pl.get("title", "Untitled"),
                          "id": {_ID_KEY: str(pl["id"])}}
                         for pl in playlists]
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching SoundCloud playlists"):
                tracks          = api.get_playlist_tracks(pl["id"][_ID_KEY])
                result[i]["songs"] = [api.track_to_songs_dict(t) for t in tracks]
            return result

        existing_pls_raw = api.get_playlists()
        existing_pls     = {pl.get("title", ""): str(pl["id"]) for pl in existing_pls_raw}

        for playlist in songs_dict:
            name            = playlist.get("name", "Untitled")
            incoming_songs  = playlist.get("songs", [])

            if len(incoming_songs) > _MAX_TRACKS:
                log.warning(f"SoundCloud: '{name}' has {len(incoming_songs)} tracks; "
                            f"truncating to {_MAX_TRACKS}.")
                incoming_songs = incoming_songs[:_MAX_TRACKS]

            pl_id = existing_pls.get(name)
            if pl_id:
                existing_tracks = api.get_playlist_tracks(pl_id)
                existing_ids    = [str(t.get("id") or (t.get("track") or {}).get("id"))
                                   for t in existing_tracks]
            else:
                try:
                    pl_id = api.create_playlist(name)
                except Exception as e:
                    log.error(f"Could not create SoundCloud playlist '{name}': {e}")
                    continue
                existing_ids = []

            new_ids = list(existing_ids)
            for song in tqdm(incoming_songs, desc=f"Matching '{name}'"):
                try:
                    mid = _match(song)
                    if mid and str(mid) not in existing_ids:
                        new_ids.append(str(mid))
                        time.sleep(0.4)
                except Exception as e:
                    log.warning(f"SoundCloud match failed for '{song.get('title')}': {e}")

            api.update_playlist_tracks(pl_id, new_ids)


def test(database, **kwargs):
    access_token = database.get("access_token", "").strip()
    if not access_token:
        raise Exception("Access token is required.")
    api = SoundCloudAPI(access_token)
    me  = api.me()
    log.info(f"SoundCloud connected: {me.get('username')}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch playlists or liked tracks from SoundCloud."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": f"Write to SoundCloud. Max {_MAX_TRACKS} tracks per playlist."},
        {"type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
         "id": "existing_playlists", "options": ["Append", "Update"], "required": True},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
