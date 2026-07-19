#!/usr/bin/env python3

"""
up_tidal

Input and output plugin for TIDAL.
Auth: OAuth2 Authorization Code flow (POST /oauth2/token).
API: https://openapi.tidal.com / legacy https://api.tidal.com/v1
ISRC/UPC: fully present. caps: PpTtAaRr all 1.
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
    "name": "tidal",
    "description": "sync playlists, favorites, albums, and artists to/from tidal",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.1",
    "settings": [
        {
            "type": "string",
            "value": "TIDAL uses OAuth2. Paste your access token and user ID from the TIDAL developer dashboard or a HAR capture. Token refresh is handled automatically when a refresh token is provided.",
        },
        {
            "type": "text",
            "label": "Access Token",
            "name": "access_token",
            "value": "",
        },
        {
            "type": "text",
            "label": "Refresh Token (optional)",
            "name": "refresh_token",
            "value": "",
        },
        {
            "type": "text",
            "label": "Client ID (for token refresh)",
            "name": "client_id",
            "value": "",
        },
        {
            "type": "text",
            "label": "Client Secret (for token refresh)",
            "name": "client_secret",
            "value": "",
        },
        {
            "type": "text",
            "label": "User ID",
            "name": "user_id",
            "value": "",
        },
        {
            "type": "text",
            "label": "Country Code",
            "name": "country_code",
            "value": "US",
        },
        {
            "type": "select",
            "label": "Sync Mode",
            "name": "sync_mode",
            "options": ["playlists", "favorites", "albums", "artists"],
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

_BASE = "https://api.tidal.com/v1"
_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
_TOKEN_CACHE_FILE = "tidal_token.json"


class TidalAPI:
    """Thin wrapper around TIDAL v1 API."""

    ID_KEY = "tidal"

    def __init__(self, access_token, user_id, country="US",
                 refresh_token=None, client_id=None, client_secret=None):
        self.access_token = access_token
        self.user_id = str(user_id)
        self.country = country
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}"}

    def _get(self, path, params=None):
        p = {"countryCode": self.country}
        if params:
            p.update(params)
        resp = requests.get(f"{_BASE}{path}", headers=self._headers(), params=p, timeout=30)
        if resp.status_code == 401 and self.refresh_token:
            self._refresh()
            resp = requests.get(f"{_BASE}{path}", headers=self._headers(), params=p, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, data=None, params=None):
        p = {"countryCode": self.country}
        if params:
            p.update(params)
        resp = requests.post(f"{_BASE}{path}", headers=self._headers(), json=data or {}, params=p, timeout=30)
        if resp.status_code == 401 and self.refresh_token:
            self._refresh()
            resp = requests.post(f"{_BASE}{path}", headers=self._headers(), json=data or {}, params=p, timeout=30)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    def _delete(self, path, params=None):
        p = {"countryCode": self.country}
        if params:
            p.update(params)
        resp = requests.delete(f"{_BASE}{path}", headers=self._headers(), params=p, timeout=30)
        resp.raise_for_status()

    def _refresh(self):
        if not all([self.refresh_token, self.client_id, self.client_secret]):
            raise Exception("Cannot refresh TIDAL token: missing client credentials.")
        resp = requests.post(
            _TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            auth=(self.client_id, self.client_secret),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]

    # ── playlists ──────────────────────────────────────────────────────────

    def get_playlists(self):
        data = self._get(f"/users/{self.user_id}/playlists", {"limit": 50})
        return data.get("items", [])

    def get_playlist_tracks(self, playlist_uuid):
        data = self._get(f"/playlists/{playlist_uuid}/tracks", {"limit": 10000})
        return data.get("items", [])

    def create_playlist(self, name):
        data = self._post(f"/users/{self.user_id}/playlists", data={"title": name, "description": ""})
        return data.get("uuid") or data.get("id")

    def playlist_add_tracks(self, playlist_uuid, track_ids):
        for i in range(0, len(track_ids), 100):
            self._post(
                f"/playlists/{playlist_uuid}/tracks",
                data={"trackIds": track_ids[i:i + 100], "toIndex": 0},
            )

    # ── favorites ──────────────────────────────────────────────────────────

    def get_favorite_tracks(self):
        data = self._get(f"/users/{self.user_id}/favorites/tracks", {"limit": 10000})
        return [item.get("item", item) for item in data.get("items", [])]

    def add_favorite_track(self, track_id):
        self._post(f"/users/{self.user_id}/favorites/tracks",
                   data={"trackId": int(track_id)})

    # ── albums ─────────────────────────────────────────────────────────────

    def get_favorite_albums(self):
        data = self._get(f"/users/{self.user_id}/favorites/albums", {"limit": 10000})
        return [item.get("item", item) for item in data.get("items", [])]

    def add_favorite_album(self, album_id):
        self._post(f"/users/{self.user_id}/favorites/albums",
                   data={"albumId": int(album_id)})

    # ── artists ────────────────────────────────────────────────────────────

    def get_favorite_artists(self):
        data = self._get(f"/users/{self.user_id}/favorites/artists", {"limit": 10000})
        return [item.get("item", item) for item in data.get("items", [])]

    def add_favorite_artist(self, artist_id):
        self._post(f"/users/{self.user_id}/favorites/artists",
                   data={"artistId": int(artist_id)})

    # ── search ─────────────────────────────────────────────────────────────

    def search(self, query, types="TRACKS", limit=10):
        data = self._get("/search", {"query": query, "types": types, "limit": limit})
        return data.get("tracks", {}).get("items", []) if types == "TRACKS" else data

    def search_albums(self, query, limit=10):
        data = self._get("/search", {"query": query, "types": "ALBUMS", "limit": limit})
        return data.get("albums", {}).get("items", [])

    def search_artists(self, query, limit=10):
        data = self._get("/search", {"query": query, "types": "ARTISTS", "limit": limit})
        return data.get("artists", {}).get("items", [])

    # ── conversion ─────────────────────────────────────────────────────────

    def track_to_songs_dict(self, track):
        d = {"title": track.get("title", "")}
        artists = [a["name"] for a in track.get("artists", [])] or ([track["artist"]["name"]] if track.get("artist") else [])
        if artists:
            d["artists"] = artists
        if track.get("album", {}).get("title"):
            d["album"] = track["album"]["title"]
        if track.get("releaseDate"):
            d["date"] = track["releaseDate"][:4]
        if track.get("isrc"):
            d["isrc"] = track["isrc"]
        d["id"] = {self.ID_KEY: str(track["id"])}
        return {k: v for k, v in d.items() if v}

    def album_to_dict(self, album):
        d = {"name": album.get("title", ""), "id": {self.ID_KEY: str(album["id"])}}
        artists = [a["name"] for a in album.get("artists", [])] or ([album["artist"]["name"]] if album.get("artist") else [])
        if artists:
            d["artists"] = artists
        if album.get("releaseDate"):
            d["date"] = album["releaseDate"][:4]
        if album.get("upc"):
            d["upc"] = album["upc"]
        return {k: v for k, v in d.items() if v}

    def artist_to_dict(self, artist):
        return {"name": artist.get("name", ""), "id": {self.ID_KEY: str(artist["id"])}}


def run(settings_dict, **kwargs):
    database = kwargs["database"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    access_token = database.get("access_token", "").strip()
    user_id = database.get("user_id", "").strip()
    country = (database.get("country_code") or "US").strip()

    if not access_token or not user_id:
        raise Exception("TIDAL plugin requires Access Token and User ID.")

    api = TidalAPI(
        access_token=access_token,
        user_id=user_id,
        country=country,
        refresh_token=database.get("refresh_token", "").strip() or None,
        client_id=database.get("client_id", "").strip() or None,
        client_secret=database.get("client_secret", "").strip() or None,
    )

    sync_mode = database.get("sync_mode", "playlists")
    fuzzy_ratio = 85
    try:
        fuzzy_ratio = float(
            settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio") or 85
        )
    except (ValueError, TypeError):
        pass

    ID_KEY = api.ID_KEY

    def _match_track(song):
        try:
            return song["id"][ID_KEY]
        except KeyError:
            pass
        for plat, pid in (song.get("id") or {}).items():
            if plat != ID_KEY:
                learned = matchings.lookup(plat, pid, ID_KEY)
                if learned:
                    return learned
        if song.get("isrc"):
            learned = matchings.lookup_by_isrc(song["isrc"], ID_KEY)
            if learned:
                return learned
        q = " ".join(filter(None, [song.get("title"), (song.get("artists") or [""])[0]]))
        if not q:
            return None
        results = api.search(q, limit=10)
        best_score, best_id = 0, None
        for r in results:
            score = fuzzymatch.similarity(song, api.track_to_songs_dict(r))
            if score > best_score:
                best_score, best_id = score, r.get("id")
        if best_score >= fuzzy_ratio and best_id:
            for plat, pid in (song.get("id") or {}).items():
                if plat != ID_KEY:
                    matchings.save(plat, pid, ID_KEY, best_id, src_isrc=song.get("isrc"),
                                   src_title=song.get("title"),
                                   src_artist="; ".join(song.get("artists", [])))
            return best_id
        return None

    if sync_mode == "favorites":
        if component == "inputs":
            tracks = api.get_favorite_tracks()
            return [{"name": "Favorites", "id": {ID_KEY: "__favorites__"},
                     "songs": [api.track_to_songs_dict(t) for t in tracks]}]
        else:
            existing = {str(t.get("id")) for t in api.get_favorite_tracks()}
            for playlist in songs_dict:
                for song in playlist.get("songs", []):
                    mid = _match_track(song)
                    if mid and str(mid) not in existing:
                        try:
                            api.add_favorite_track(mid)
                            existing.add(str(mid))
                        except Exception as e:
                            log.warning(f"TIDAL favorite add failed: {e}")

    elif sync_mode == "albums":
        if component == "inputs":
            albums = api.get_favorite_albums()
            return [{"name": "Albums", "id": {ID_KEY: "__albums__"},
                     "songs": [api.album_to_dict(a) for a in albums]}]
        else:
            existing = {str(a.get("id")) for a in api.get_favorite_albums()}
            for playlist in songs_dict:
                for album_item in playlist.get("songs", []):
                    alb_id = album_item.get("id", {}).get(ID_KEY)
                    if not alb_id:
                        upc = album_item.get("upc")
                        if upc:
                            results = api.search_albums(f"upc:{upc}", limit=1)
                            if results:
                                alb_id = str(results[0]["id"])
                        if not alb_id:
                            name = album_item.get("name", "")
                            artist = (album_item.get("artists") or [""])[0]
                            results = api.search_albums(f"{artist} {name}".strip(), limit=5)
                            if results:
                                alb_id = str(results[0]["id"])
                    if alb_id and alb_id not in existing:
                        try:
                            api.add_favorite_album(alb_id)
                            existing.add(alb_id)
                        except Exception as e:
                            log.warning(f"TIDAL album add failed: {e}")

    elif sync_mode == "artists":
        if component == "inputs":
            artists = api.get_favorite_artists()
            return [{"name": "Artists", "id": {ID_KEY: "__artists__"},
                     "songs": [api.artist_to_dict(a) for a in artists]}]
        else:
            existing = {str(a.get("id")) for a in api.get_favorite_artists()}
            for playlist in songs_dict:
                for artist_item in playlist.get("songs", []):
                    art_id = artist_item.get("id", {}).get(ID_KEY)
                    if not art_id:
                        name = artist_item.get("name", "")
                        results = api.search_artists(name, limit=1)
                        if results:
                            art_id = str(results[0]["id"])
                    if art_id and art_id not in existing:
                        try:
                            api.add_favorite_artist(art_id)
                            existing.add(art_id)
                        except Exception as e:
                            log.warning(f"TIDAL artist follow failed: {e}")

    else:
        # playlists
        if component == "inputs":
            playlists = api.get_playlists()
            result = []
            for pl in playlists:
                uuid = pl.get("uuid") or pl.get("id")
                result.append({"name": pl.get("title", "Untitled"), "id": {ID_KEY: uuid}})
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching TIDAL playlists"):
                tracks = api.get_playlist_tracks(pl["id"][ID_KEY])
                result[i]["songs"] = [api.track_to_songs_dict(t) for t in tracks]
            return result
        else:
            existing_pls = {pl.get("title", ""): (pl.get("uuid") or pl.get("id")) for pl in api.get_playlists()}
            for playlist in songs_dict:
                name = playlist.get("name", "Untitled")
                pl_id = existing_pls.get(name) or api.create_playlist(name)
                if not pl_id:
                    log.error(f"Could not create TIDAL playlist '{name}'")
                    continue
                existing_tracks = api.get_playlist_tracks(pl_id)
                existing_ids = {str(t.get("id")) for t in existing_tracks}
                to_add = []
                for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                    mid = _match_track(song)
                    if mid and str(mid) not in existing_ids:
                        to_add.append(int(mid))
                if to_add:
                    api.playlist_add_tracks(pl_id, to_add)


def test(database, **kwargs):
    access_token = database.get("access_token", "").strip()
    user_id = database.get("user_id", "").strip()
    if not access_token or not user_id:
        raise Exception("Access Token and User ID are required.")
    api = TidalAPI(access_token=access_token, user_id=user_id,
                   country=database.get("country_code", "US").strip())
    api._get(f"/users/{user_id}")
    log.info(f"TIDAL connection OK for user {user_id}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch from TIDAL."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": "Write to TIDAL."},
        {
            "type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
            "id": "existing_playlists", "options": ["Append", "Update"], "required": True,
        },
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
