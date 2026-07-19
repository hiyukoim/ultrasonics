#!/usr/bin/env python3

"""
up_qobuz

Input and output plugin for Qobuz.
Auth: app_id + app_secret + user login → user_auth_token.
API: https://www.qobuz.com/api.json/0.2
ISRC/UPC: fully present. caps: PpTtAaRr all 1. max=1999.
"""

import hashlib
import time

import requests
from tqdm import tqdm

from app import _ultrasonics
from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter

log = logs.create_log(__name__)

handshake = {
    "name": "qobuz",
    "description": "sync playlists, favorites, albums, and artists to/from qobuz",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.1",
    "settings": [
        {
            "type": "string",
            "value": "Qobuz requires an app_id, app_secret, and your account credentials.",
        },
        {
            "type": "text",
            "label": "App ID",
            "name": "app_id",
            "value": "",
        },
        {
            "type": "text",
            "label": "App Secret",
            "name": "app_secret",
            "value": "",
        },
        {
            "type": "text",
            "label": "Email / Username",
            "name": "username",
            "value": "",
        },
        {
            "type": "text",
            "label": "Password",
            "name": "password",
            "value": "",
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

_BASE = "https://www.qobuz.com/api.json/0.2"


class QobuzAPI:
    """Thin wrapper around the Qobuz REST API."""

    ID_KEY = "qobuz"

    def __init__(self, app_id, app_secret, username, password):
        self.app_id = app_id
        self.app_secret = app_secret
        self.username = username
        self.password = password
        self.user_auth_token = None
        self.user_id = None

    def login(self):
        """Authenticate and obtain user_auth_token."""
        pwd_md5 = hashlib.md5(self.password.encode()).hexdigest()
        resp = requests.get(
            f"{_BASE}/user/login",
            params={
                "app_id": self.app_id,
                "username": self.username,
                "password": pwd_md5,
                "email": self.username,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            raise Exception(f"Qobuz login error: {data.get('message')}")
        self.user_auth_token = data["user_auth_token"]
        self.user_id = str(data["user"]["id"])

    def _get(self, path, params=None):
        p = {"app_id": self.app_id, "user_auth_token": self.user_auth_token}
        if params:
            p.update(params)
        resp = requests.get(f"{_BASE}{path}", params=p, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            raise Exception(f"Qobuz API error: {data.get('message')}")
        return data

    def _post(self, path, data=None):
        payload = {"app_id": self.app_id, "user_auth_token": self.user_auth_token}
        if data:
            payload.update(data)
        resp = requests.post(f"{_BASE}{path}", data=payload, timeout=30)
        resp.raise_for_status()
        try:
            result = resp.json()
            if result.get("status") == "error":
                raise Exception(f"Qobuz API error: {result.get('message')}")
            return result
        except Exception:
            return {}

    # ── playlists ──────────────────────────────────────────────────────────

    def get_playlists(self):
        data = self._get("/playlist/getUserPlaylists", {"limit": 500, "offset": 0})
        return data.get("playlists", {}).get("items", [])

    def get_playlist_tracks(self, playlist_id):
        all_tracks = []
        offset = 0
        while True:
            data = self._get("/playlist/get", {"playlist_id": playlist_id,
                                                "extra": "tracks", "limit": 50, "offset": offset})
            tracks = data.get("tracks", {}).get("items", [])
            if not tracks:
                break
            all_tracks.extend(tracks)
            offset += len(tracks)
            if len(tracks) < 50:
                break
        return all_tracks

    def create_playlist(self, name):
        data = self._post("/playlist/create", {"name": name, "is_public": False})
        return str(data.get("id", ""))

    def playlist_add_tracks(self, playlist_id, track_ids):
        if not track_ids:
            return
        for i in range(0, len(track_ids), 50):
            self._post("/playlist/addTracks", {
                "playlist_id": playlist_id,
                "track_ids": ",".join(str(tid) for tid in track_ids[i:i + 50]),
            })

    # ── favorites ──────────────────────────────────────────────────────────

    def get_favorites(self, item_type="tracks"):
        all_items = []
        offset = 0
        while True:
            data = self._get("/favorite/getUserFavorites",
                             {"type": item_type, "limit": 50, "offset": offset})
            items = data.get(item_type, {}).get("items", [])
            if not items:
                break
            all_items.extend(items)
            offset += len(items)
            if len(items) < 50:
                break
        return all_items

    def add_favorite(self, item_id, item_type="track"):
        self._post("/favorite/create", {f"{item_type}_ids": str(item_id)})

    # ── search ─────────────────────────────────────────────────────────────

    def search_tracks(self, query, limit=10):
        data = self._get("/catalog/search", {"query": query, "type": "tracks", "limit": limit})
        return data.get("tracks", {}).get("items", [])

    def search_albums(self, query, limit=10):
        data = self._get("/catalog/search", {"query": query, "type": "albums", "limit": limit})
        return data.get("albums", {}).get("items", [])

    def search_artists(self, query, limit=10):
        data = self._get("/catalog/search", {"query": query, "type": "artists", "limit": limit})
        return data.get("artists", {}).get("items", [])

    # ── conversion ─────────────────────────────────────────────────────────

    def track_to_songs_dict(self, track):
        d = {"title": track.get("title", "")}
        performers = track.get("performer") or track.get("composer")
        if performers:
            d["artists"] = [performers.get("name", "")]
        if track.get("album", {}).get("title"):
            d["album"] = track["album"]["title"]
        if track.get("album", {}).get("release_date_original"):
            d["date"] = track["album"]["release_date_original"][:4]
        if track.get("isrc"):
            d["isrc"] = track["isrc"]
        d["id"] = {self.ID_KEY: str(track["id"])}
        return {k: v for k, v in d.items() if v}

    def album_to_dict(self, album):
        d = {"name": album.get("title", ""), "id": {self.ID_KEY: str(album["id"])}}
        if album.get("artist", {}).get("name"):
            d["artists"] = [album["artist"]["name"]]
        if album.get("release_date_original"):
            d["date"] = album["release_date_original"][:4]
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

    app_id = database.get("app_id", "").strip()
    app_secret = database.get("app_secret", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")

    if not all([app_id, app_secret, username, password]):
        raise Exception("Qobuz plugin requires app_id, app_secret, username, and password.")

    api = QobuzAPI(app_id, app_secret, username, password)
    try:
        api.login()
    except Exception as e:
        raise Exception(f"Qobuz login failed: {e}")

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
        results = api.search_tracks(q, limit=10)
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
            tracks = api.get_favorites("tracks")
            return [{"name": "Favorites", "id": {ID_KEY: "__favorites__"},
                     "songs": [api.track_to_songs_dict(t) for t in tracks]}]
        else:
            existing = {str(t.get("id")) for t in api.get_favorites("tracks")}
            for playlist in songs_dict:
                for song in playlist.get("songs", []):
                    mid = _match_track(song)
                    if mid and str(mid) not in existing:
                        try:
                            api.add_favorite(mid, "track")
                            existing.add(str(mid))
                        except Exception as e:
                            log.warning(f"Qobuz favorite failed: {e}")

    elif sync_mode == "albums":
        if component == "inputs":
            albums = api.get_favorites("albums")
            return [{"name": "Albums", "id": {ID_KEY: "__albums__"},
                     "songs": [api.album_to_dict(a) for a in albums]}]
        else:
            existing = {str(a.get("id")) for a in api.get_favorites("albums")}
            for playlist in songs_dict:
                for album_item in playlist.get("songs", []):
                    alb_id = album_item.get("id", {}).get(ID_KEY)
                    if not alb_id:
                        upc = album_item.get("upc")
                        if upc:
                            results = api.search_albums(upc, limit=1)
                            if results and results[0].get("upc") == upc:
                                alb_id = str(results[0]["id"])
                        if not alb_id:
                            name = album_item.get("name", "")
                            artist = (album_item.get("artists") or [""])[0]
                            results = api.search_albums(f"{artist} {name}".strip(), limit=5)
                            if results:
                                alb_id = str(results[0]["id"])
                    if alb_id and alb_id not in existing:
                        try:
                            api.add_favorite(alb_id, "album")
                            existing.add(alb_id)
                        except Exception as e:
                            log.warning(f"Qobuz album add failed: {e}")

    elif sync_mode == "artists":
        if component == "inputs":
            artists = api.get_favorites("artists")
            return [{"name": "Artists", "id": {ID_KEY: "__artists__"},
                     "songs": [api.artist_to_dict(a) for a in artists]}]
        else:
            existing = {str(a.get("id")) for a in api.get_favorites("artists")}
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
                            api.add_favorite(art_id, "artist")
                            existing.add(art_id)
                        except Exception as e:
                            log.warning(f"Qobuz artist follow failed: {e}")

    else:
        # playlists
        if component == "inputs":
            playlists = api.get_playlists()
            result = [{"name": pl.get("name", "Untitled"), "id": {ID_KEY: str(pl["id"])}} for pl in playlists]
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching Qobuz playlists"):
                tracks = api.get_playlist_tracks(pl["id"][ID_KEY])
                result[i]["songs"] = [api.track_to_songs_dict(t) for t in tracks]
            return result
        else:
            existing_pls = {pl.get("name", ""): str(pl["id"]) for pl in api.get_playlists()}
            for playlist in songs_dict:
                name = playlist.get("name", "Untitled")
                pl_id = existing_pls.get(name) or api.create_playlist(name)
                if not pl_id:
                    log.error(f"Could not create Qobuz playlist '{name}'")
                    continue
                existing_tracks = api.get_playlist_tracks(pl_id)
                existing_ids = {str(t.get("id")) for t in existing_tracks}
                to_add = []
                for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                    mid = _match_track(song)
                    if mid and str(mid) not in existing_ids:
                        to_add.append(mid)
                api.playlist_add_tracks(pl_id, to_add)


def test(database, **kwargs):
    app_id = database.get("app_id", "").strip()
    app_secret = database.get("app_secret", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")
    if not all([app_id, app_secret, username, password]):
        raise Exception("app_id, app_secret, username, and password are required.")
    api = QobuzAPI(app_id, app_secret, username, password)
    api.login()
    log.info(f"Qobuz connection OK for {username}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch from Qobuz."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": "Write to Qobuz (max 1999 tracks per playlist)."},
        {
            "type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
            "id": "existing_playlists", "options": ["Append", "Update"], "required": True,
        },
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
