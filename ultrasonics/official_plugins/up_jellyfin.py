#!/usr/bin/env python3

"""
up_jellyfin

Input and output plugin for Jellyfin (and Emby; see up_emby.py).
Auth: POST /Users/AuthenticateByName → AccessToken.
API: /Items (Audio), /Playlists, /Playlists/{id}/Items.
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
    "name": "jellyfin",
    "description": "sync playlists, favorites, albums, and artists to/from a jellyfin server",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.1",
    "settings": [
        {
            "type": "text",
            "label": "Server URL",
            "name": "server_url",
            "value": "e.g. http://localhost:8096",
        },
        {
            "type": "text",
            "label": "Username",
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

# Server identifier sent in Emby-Authorization header
_CLIENT_NAME = "ultrasonics"
_DEVICE_NAME = "ultrasonics"
_DEVICE_ID = "ultrasonics-jellyfin"
_CLIENT_VERSION = "0.1"


class JellyfinAPI:
    """Thin wrapper around the Jellyfin/Emby HTTP API."""

    ID_KEY = "jellyfin"

    def __init__(self, server_url, username, password):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = None
        self.user_id = None

    def _auth_header(self):
        parts = [
            f'MediaBrowser Client="{_CLIENT_NAME}"',
            f'Device="{_DEVICE_NAME}"',
            f'DeviceId="{_DEVICE_ID}"',
            f'Version="{_CLIENT_VERSION}"',
        ]
        if self.token:
            parts.append(f'Token="{self.token}"')
        return {"X-Emby-Authorization": ", ".join(parts)}

    def authenticate(self):
        url = f"{self.server_url}/Users/AuthenticateByName"
        payload = {"Username": self.username, "Pw": self.password}
        resp = requests.post(url, json=payload, headers=self._auth_header(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self.token = data["AccessToken"]
        self.user_id = data["User"]["Id"]

    def _get(self, path, params=None):
        resp = requests.get(
            f"{self.server_url}{path}",
            params=params or {},
            headers=self._auth_header(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, body=None, params=None):
        resp = requests.post(
            f"{self.server_url}{path}",
            json=body or {},
            params=params or {},
            headers=self._auth_header(),
            timeout=30,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    def _delete(self, path, params=None):
        resp = requests.delete(
            f"{self.server_url}{path}",
            params=params or {},
            headers=self._auth_header(),
            timeout=30,
        )
        resp.raise_for_status()

    # ── playlists ──────────────────────────────────────────────────────────

    def get_playlists(self):
        data = self._get(
            f"/Users/{self.user_id}/Items",
            {"IncludeItemTypes": "Playlist", "Recursive": "true", "Fields": "Id,Name"},
        )
        return data.get("Items", [])

    def get_playlist_items(self, playlist_id):
        data = self._get(
            f"/Playlists/{playlist_id}/Items",
            {"UserId": self.user_id, "Fields": "Name,Artists,Album,ProductionYear,MediaSources,ProviderIds"},
        )
        return data.get("Items", [])

    def create_playlist(self, name):
        body = {"Name": name, "UserId": self.user_id, "MediaType": "Audio"}
        data = self._post("/Playlists", body=body)
        return data.get("Id")

    def playlist_add_items(self, playlist_id, item_ids):
        if not item_ids:
            return
        self._post(
            f"/Playlists/{playlist_id}/Items",
            params={"ids": ",".join(item_ids), "userId": self.user_id},
        )

    def playlist_remove_items(self, playlist_id, entry_ids):
        if not entry_ids:
            return
        self._delete(
            f"/Playlists/{playlist_id}/Items",
            params={"EntryIds": ",".join(entry_ids)},
        )

    # ── favorites ──────────────────────────────────────────────────────────

    def get_favorites(self):
        data = self._get(
            f"/Users/{self.user_id}/Items",
            {"IsFavorite": "true", "IncludeItemTypes": "Audio", "Recursive": "true",
             "Fields": "Name,Artists,Album,ProductionYear,ProviderIds"},
        )
        return data.get("Items", [])

    def set_favorite(self, item_id, is_favorite=True):
        method = requests.post if is_favorite else requests.delete
        resp = method(
            f"{self.server_url}/Users/{self.user_id}/FavoriteItems/{item_id}",
            headers=self._auth_header(),
            timeout=10,
        )
        resp.raise_for_status()

    # ── albums ─────────────────────────────────────────────────────────────

    def get_albums(self):
        data = self._get(
            f"/Users/{self.user_id}/Items",
            {"IncludeItemTypes": "MusicAlbum", "Recursive": "true",
             "Fields": "Name,Artists,ProductionYear,ProviderIds"},
        )
        return data.get("Items", [])

    # ── artists ────────────────────────────────────────────────────────────

    def get_artists(self):
        data = self._get(
            "/Artists",
            {"UserId": self.user_id, "Fields": "Name,Genres"},
        )
        return data.get("Items", [])

    # ── search ─────────────────────────────────────────────────────────────

    def search_audio(self, query, limit=10):
        data = self._get(
            f"/Users/{self.user_id}/Items",
            {"SearchTerm": query, "IncludeItemTypes": "Audio", "Recursive": "true",
             "Limit": limit, "Fields": "Name,Artists,Album,ProductionYear,ProviderIds"},
        )
        return data.get("Items", [])

    # ── conversion helpers ─────────────────────────────────────────────────

    def item_to_songs_dict(self, item):
        title = item.get("Name", "")
        artists = item.get("Artists", []) or [item.get("AlbumArtist", "")]
        artists = [a for a in artists if a]
        d = {"title": title}
        if artists:
            d["artists"] = artists
        if item.get("Album"):
            d["album"] = item["Album"]
        if item.get("ProductionYear"):
            d["date"] = str(item["ProductionYear"])
        provider_ids = item.get("ProviderIds", {})
        isrc = provider_ids.get("isrc") or provider_ids.get("Isrc")
        if isrc:
            d["isrc"] = isrc
        d["id"] = {self.ID_KEY: item["Id"]}
        return {k: v for k, v in d.items() if v}

    def album_to_dict(self, item):
        d = {
            "name": item.get("Name", ""),
            "id": {self.ID_KEY: item["Id"]},
        }
        artists = item.get("Artists", []) or [item.get("AlbumArtist", "")]
        if artists:
            d["artists"] = [a for a in artists if a]
        if item.get("ProductionYear"):
            d["date"] = str(item["ProductionYear"])
        provider_ids = item.get("ProviderIds", {})
        upc = provider_ids.get("upc") or provider_ids.get("Upc")
        if upc:
            d["upc"] = upc
        return {k: v for k, v in d.items() if v}

    def artist_to_dict(self, item):
        d = {"name": item.get("Name", ""), "id": {self.ID_KEY: item["Id"]}}
        if item.get("Genres"):
            d["genres"] = item["Genres"]
        return {k: v for k, v in d.items() if v}


def run(settings_dict, **kwargs):
    database = kwargs["database"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")

    if not all([server_url, username, password]):
        raise Exception("Jellyfin plugin requires server URL, username, and password.")

    api = JellyfinAPI(server_url, username, password)
    try:
        api.authenticate()
    except Exception as e:
        raise Exception(f"Jellyfin authentication failed: {e}")

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
        if song.get("isrc"):
            learned = matchings.lookup_by_isrc(song["isrc"], ID_KEY)
            if learned:
                return learned
        q = " ".join(filter(None, [song.get("title"), (song.get("artists") or [""])[0]]))
        if not q:
            return None
        results = api.search_audio(q, limit=10)
        best_score, best_id = 0, None
        for r in results:
            score = fuzzymatch.similarity(song, api.item_to_songs_dict(r))
            if score > best_score:
                best_score, best_id = score, r.get("Id")
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
            favs = api.get_favorites()
            tracks = [api.item_to_songs_dict(f) for f in favs]
            return [{"name": "Favorites", "id": {ID_KEY: "__favorites__"}, "songs": tracks}]
        else:
            existing_ids = {f["Id"] for f in api.get_favorites()}
            for playlist in songs_dict:
                for song in playlist.get("songs", []):
                    mid = _match(song)
                    if mid and mid not in existing_ids:
                        try:
                            api.set_favorite(mid, True)
                            existing_ids.add(mid)
                        except Exception as e:
                            log.warning(f"set_favorite failed: {e}")

    elif sync_mode == "albums":
        if component == "inputs":
            albums = api.get_albums()
            return [{"name": "Albums", "id": {ID_KEY: "__albums__"},
                     "songs": [api.album_to_dict(a) for a in albums]}]
        else:
            log.info("Jellyfin albums output: matching not implemented (read-only library).")

    elif sync_mode == "artists":
        if component == "inputs":
            artists = api.get_artists()
            return [{"name": "Artists", "id": {ID_KEY: "__artists__"},
                     "songs": [api.artist_to_dict(a) for a in artists]}]
        else:
            log.info("Jellyfin artists output: matching not implemented (read-only library).")

    else:
        # playlists
        if component == "inputs":
            playlists = api.get_playlists()
            result = [{"name": pl.get("Name", ""), "id": {ID_KEY: pl["Id"]}} for pl in playlists]
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching Jellyfin playlists"):
                items = api.get_playlist_items(pl["id"][ID_KEY])
                result[i]["songs"] = [api.item_to_songs_dict(it) for it in items]
            return result
        else:
            existing_pls = {pl.get("Name", ""): pl["Id"] for pl in api.get_playlists()}
            for playlist in songs_dict:
                name = playlist.get("name", "Untitled")
                pl_id = existing_pls.get(name)
                if not pl_id:
                    pl_id = api.create_playlist(name)
                if not pl_id:
                    log.error(f"Could not create playlist '{name}'")
                    continue
                existing_items = api.get_playlist_items(pl_id)
                existing_tracks = [api.item_to_songs_dict(it) for it in existing_items]
                existing_ids = [it.get("Id") for it in existing_items]

                to_add = []
                for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                    if fuzzymatch.duplicate(song, existing_tracks, fuzzy_ratio):
                        continue
                    mid = _match(song)
                    if mid and mid not in existing_ids:
                        to_add.append(mid)
                api.playlist_add_items(pl_id, to_add)


def test(database, **kwargs):
    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")
    if not all([server_url, username, password]):
        raise Exception("Server URL, username, and password are required.")
    api = JellyfinAPI(server_url, username, password)
    api.authenticate()
    log.info(f"Jellyfin connection OK: {server_url}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch from Jellyfin."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": "Write to Jellyfin."},
        {
            "type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
            "id": "existing_playlists", "options": ["Append", "Update"], "required": True,
        },
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
