#!/usr/bin/env python3

"""
up_soundcloud

Input and output plugin for SoundCloud.
Auth: OAuth2 Authorization Code + PKCE (official API).
ISRC: partial (UGC — many tracks lack it). Match by title+artist.
caps: PpTtAaRr all 1. max: 500 (extremely low!). tpt: ~0.41s.
"""

import json
import os
import time

import requests
from tqdm import tqdm

from app import _ultrasonics
from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter

log = logs.create_log(__name__)

handshake = {
    "name": "soundcloud",
    "description": "sync playlists and favorites to/from soundcloud (max 500 tracks/playlist)",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites"],
    "version": "0.1",
    "settings": [
        {
            "type": "string",
            "value": "SoundCloud uses OAuth2. Paste your access token and client ID below. Note: playlists are capped at 500 tracks.",
        },
        {
            "type": "text",
            "label": "Access Token",
            "name": "access_token",
            "value": "",
        },
        {
            "type": "text",
            "label": "Client ID (for unauthenticated endpoints)",
            "name": "client_id",
            "value": "",
        },
        {
            "type": "select",
            "label": "Sync Mode",
            "name": "sync_mode",
            "options": ["playlists", "favorites"],
            "value": "playlists",
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": "Recommended: 85",
        },
    ],
}

_BASE = "https://api.soundcloud.com"
_MAX_TRACKS = 500


class SoundCloudAPI:
    """Thin wrapper around the SoundCloud REST API."""

    ID_KEY = "soundcloud"

    def __init__(self, access_token, client_id=None):
        self.access_token = access_token
        self.client_id = client_id
        self._me = None

    def _headers(self):
        return {
            "Authorization": f"OAuth {self.access_token}",
            "Accept": "application/json; charset=utf-8",
        }

    def _get(self, path, params=None):
        p = params or {}
        resp = requests.get(f"{_BASE}{path}", headers=self._headers(), params=p, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, json_body=None, params=None):
        resp = requests.post(
            f"{_BASE}{path}",
            headers=self._headers(),
            json=json_body or {},
            params=params or {},
            timeout=30,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    def _put(self, path, json_body=None, params=None):
        resp = requests.put(
            f"{_BASE}{path}",
            headers=self._headers(),
            json=json_body or {},
            params=params or {},
            timeout=30,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    def me(self):
        if not self._me:
            self._me = self._get("/me")
        return self._me

    # ── playlists ──────────────────────────────────────────────────────────

    def get_playlists(self):
        user_id = self.me()["id"]
        return self._get(f"/users/{user_id}/playlists")

    def get_playlist_tracks(self, playlist_id):
        data = self._get(f"/playlists/{playlist_id}", {"representation": "compact"})
        tracks = data.get("tracks", [])
        if tracks and isinstance(tracks[0], dict) and not tracks[0].get("title"):
            # compact form — need to hydrate
            ids = [str(t["id"]) for t in tracks[:_MAX_TRACKS]]
            return self._hydrate_tracks(ids)
        return tracks[:_MAX_TRACKS]

    def _hydrate_tracks(self, track_ids):
        """Fetch full track objects for a list of IDs (max 50 per request)."""
        results = []
        for i in range(0, len(track_ids), 50):
            chunk = track_ids[i:i + 50]
            try:
                data = self._get("/tracks", {"ids": ",".join(chunk)})
                if isinstance(data, list):
                    results.extend(data)
                else:
                    results.extend(data.get("collection", []))
            except Exception as e:
                log.warning(f"SoundCloud hydrate failed: {e}")
        return results

    def create_playlist(self, name):
        data = self._post("/playlists", {"playlist": {"title": name, "sharing": "private"}})
        return str(data.get("id", ""))

    def update_playlist_tracks(self, playlist_id, track_ids):
        tracks = [{"id": int(tid)} for tid in track_ids[:_MAX_TRACKS]]
        self._put(f"/playlists/{playlist_id}", {"playlist": {"tracks": tracks}})

    # ── favorites (likes) ──────────────────────────────────────────────────

    def get_likes(self):
        user_id = self.me()["id"]
        all_items = []
        url = f"{_BASE}/users/{user_id}/track_likes"
        params = {"limit": 200}
        while url:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("collection", data if isinstance(data, list) else [])
            all_items.extend(items)
            # SoundCloud uses next_href for pagination
            url = data.get("next_href") if isinstance(data, dict) else None
            params = {}
        return all_items

    def like_track(self, track_id):
        user_id = self.me()["id"]
        requests.put(
            f"{_BASE}/users/{user_id}/track_likes/{track_id}",
            headers=self._headers(),
            timeout=15,
        ).raise_for_status()

    # ── search ─────────────────────────────────────────────────────────────

    def search_tracks(self, query, limit=10):
        data = self._get("/tracks", {"q": query, "limit": limit})
        if isinstance(data, list):
            return data
        return data.get("collection", [])

    # ── conversion ─────────────────────────────────────────────────────────

    def track_to_songs_dict(self, track):
        if isinstance(track, dict) and "track" in track:
            track = track["track"]
        title = track.get("title", "")
        artist = track.get("user", {}).get("username") or track.get("user_name") or ""
        d = {"title": title}
        if artist:
            d["artists"] = [artist]
        if track.get("id"):
            d["id"] = {self.ID_KEY: str(track["id"])}
        return {k: v for k, v in d.items() if v}


def run(settings_dict, **kwargs):
    database = kwargs["database"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    access_token = database.get("access_token", "").strip()
    client_id = database.get("client_id", "").strip() or None

    if not access_token:
        raise Exception("SoundCloud access token is required.")

    api = SoundCloudAPI(access_token, client_id)

    # Validate token
    try:
        api.me()
    except Exception as e:
        raise Exception(f"SoundCloud auth failed: {e}")

    sync_mode = database.get("sync_mode", "playlists")
    fuzzy_ratio = 85
    try:
        fuzzy_ratio = float(
            settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio") or 85
        )
    except (ValueError, TypeError):
        pass

    ID_KEY = api.ID_KEY

    def _match(song):
        try:
            return song["id"][ID_KEY]
        except KeyError:
            pass
        for plat, pid in (song.get("id") or {}).items():
            if plat != ID_KEY:
                learned = matchings.lookup(plat, pid, ID_KEY)
                if learned:
                    return learned
        q = " ".join(filter(None, [song.get("title"), (song.get("artists") or [""])[0]]))
        if not q:
            return None
        results = api.search_tracks(q, limit=10)
        best_score, best_id = 0, None
        for r in results:
            score = fuzzymatch.similarity(song, api.track_to_songs_dict(r))
            if score > best_score:
                best_score, best_id = score, r.get("id")
        if best_score >= fuzzy_ratio and best_id:
            for plat, pid in (song.get("id") or {}).items():
                if plat != ID_KEY:
                    matchings.save(plat, pid, ID_KEY, best_id,
                                   src_title=song.get("title"),
                                   src_artist="; ".join(song.get("artists", [])))
            return best_id
        return None

    if sync_mode == "favorites":
        if component == "inputs":
            liked = api.get_likes()
            tracks = [api.track_to_songs_dict(t) for t in liked]
            return [{"name": "Favorites", "id": {ID_KEY: "__favorites__"}, "songs": tracks}]
        else:
            for playlist in songs_dict:
                for song in playlist.get("songs", []):
                    mid = _match(song)
                    if mid:
                        try:
                            api.like_track(mid)
                            time.sleep(0.5)
                        except Exception as e:
                            log.warning(f"SoundCloud like failed: {e}")

    else:
        # playlists
        if component == "inputs":
            playlists = api.get_playlists()
            result = []
            for pl in playlists:
                result.append({"name": pl.get("title", "Untitled"), "id": {ID_KEY: str(pl["id"])}})
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching SoundCloud playlists"):
                tracks = api.get_playlist_tracks(pl["id"][ID_KEY])
                result[i]["songs"] = [api.track_to_songs_dict(t) for t in tracks]
            return result
        else:
            existing_pls_raw = api.get_playlists()
            existing_pls = {pl.get("title", ""): str(pl["id"]) for pl in existing_pls_raw}

            for playlist in songs_dict:
                name = playlist.get("name", "Untitled")
                incoming_songs = playlist.get("songs", [])

                if len(incoming_songs) > _MAX_TRACKS:
                    log.warning(
                        f"SoundCloud max is {_MAX_TRACKS} tracks; playlist '{name}' "
                        f"has {len(incoming_songs)} — truncating."
                    )
                    incoming_songs = incoming_songs[:_MAX_TRACKS]

                pl_id = existing_pls.get(name)
                if pl_id:
                    # Append mode: read existing IDs then combine
                    existing_tracks = api.get_playlist_tracks(pl_id)
                    existing_ids = [str(t.get("id") or t.get("track", {}).get("id")) for t in existing_tracks]
                else:
                    pl_id = api.create_playlist(name)
                    existing_ids = []

                if not pl_id:
                    log.error(f"Could not create SoundCloud playlist '{name}'")
                    continue

                new_ids = list(existing_ids)
                for song in tqdm(incoming_songs, desc=f"Matching '{name}'"):
                    mid = _match(song)
                    if mid and str(mid) not in existing_ids:
                        new_ids.append(str(mid))
                        time.sleep(0.5)

                api.update_playlist_tracks(pl_id, new_ids)


def test(database, **kwargs):
    access_token = database.get("access_token", "").strip()
    if not access_token:
        raise Exception("Access token is required.")
    api = SoundCloudAPI(access_token)
    me = api.me()
    log.info(f"SoundCloud connection OK: {me.get('username')}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch playlists or liked tracks from SoundCloud."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": f"Write to SoundCloud. Max {_MAX_TRACKS} tracks per playlist."},
        {
            "type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
            "id": "existing_playlists", "options": ["Append", "Update"], "required": True,
        },
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
