#!/usr/bin/env python3

"""
up_jellyfin

Input and output plugin for Jellyfin media server.
Auth: POST /Users/AuthenticateByName → AccessToken.
Vault contract: on successful match of a previously orphaned track,
calls vault.flip_orphan_to_linked(). Failures are isolated per-song.
"""

import requests
from tqdm import tqdm

from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter

log = logs.create_log(__name__)

handshake = {
    "name": "jellyfin",
    "description": "sync playlists, favorites, albums, and artists to/from a jellyfin server",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.2",
    "settings": [
        {"type": "text",   "label": "Server URL", "name": "server_url", "value": "e.g. http://localhost:8096"},
        {"type": "text",   "label": "Username",   "name": "username",   "value": ""},
        {"type": "text",   "label": "Password",   "name": "password",   "value": ""},
        {"type": "select", "label": "Sync Mode",  "name": "sync_mode",
         "options": ["playlists", "favorites", "albums", "artists"], "value": "playlists"},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": "85"},
    ],
}

_CLIENT = "ultrasonics"
_DEVICE = "ultrasonics"
_DEVICE_ID = "ultrasonics-jellyfin"
_VERSION = "0.2"


def _parse_ratio(val, default=85.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class JellyfinAPI:
    ID_KEY = "jellyfin"

    def __init__(self, server_url, username, password):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = None
        self.user_id = None

    def _auth_header(self):
        parts = [
            f'MediaBrowser Client="{_CLIENT}"',
            f'Device="{_DEVICE}"',
            f'DeviceId="{_DEVICE_ID}"',
            f'Version="{_VERSION}"',
        ]
        if self.token:
            parts.append(f'Token="{self.token}"')
        return {"X-Emby-Authorization": ", ".join(parts)}

    def authenticate(self):
        resp = requests.post(
            f"{self.server_url}/Users/AuthenticateByName",
            json={"Username": self.username, "Pw": self.password},
            headers=self._auth_header(), timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token   = data["AccessToken"]
        self.user_id = data["User"]["Id"]

    def _get(self, path, params=None):
        resp = requests.get(f"{self.server_url}{path}", params=params or {},
                            headers=self._auth_header(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, body=None, params=None):
        resp = requests.post(f"{self.server_url}{path}", json=body or {},
                             params=params or {}, headers=self._auth_header(), timeout=30)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    def _delete(self, path, params=None):
        requests.delete(f"{self.server_url}{path}", params=params or {},
                        headers=self._auth_header(), timeout=30).raise_for_status()

    # ── playlists ─────────────────────────────────────────────────────────────

    def get_playlists(self):
        data = self._get(f"/Users/{self.user_id}/Items",
                         {"IncludeItemTypes": "Playlist", "Recursive": "true", "Fields": "Id,Name"})
        return data.get("Items", [])

    def get_playlist_items(self, playlist_id):
        data = self._get(f"/Playlists/{playlist_id}/Items",
                         {"UserId": self.user_id,
                          "Fields": "Name,Artists,Album,ProductionYear,ProviderIds"})
        return data.get("Items", [])

    def create_playlist(self, name):
        data = self._post("/Playlists",
                          body={"Name": name, "UserId": self.user_id, "MediaType": "Audio"})
        return data.get("Id")

    def playlist_add_items(self, playlist_id, item_ids):
        if not item_ids:
            return
        self._post(f"/Playlists/{playlist_id}/Items",
                   params={"ids": ",".join(item_ids), "userId": self.user_id})

    def playlist_remove_items(self, playlist_id, entry_ids):
        if not entry_ids:
            return
        self._delete(f"/Playlists/{playlist_id}/Items",
                     params={"EntryIds": ",".join(entry_ids)})

    # ── favorites ─────────────────────────────────────────────────────────────

    def get_favorites(self):
        data = self._get(f"/Users/{self.user_id}/Items",
                         {"IsFavorite": "true", "IncludeItemTypes": "Audio",
                          "Recursive": "true",
                          "Fields": "Name,Artists,Album,ProductionYear,ProviderIds"})
        return data.get("Items", [])

    def set_favorite(self, item_id, is_favorite=True):
        method = requests.post if is_favorite else requests.delete
        method(f"{self.server_url}/Users/{self.user_id}/FavoriteItems/{item_id}",
               headers=self._auth_header(), timeout=10).raise_for_status()

    # ── albums / artists ──────────────────────────────────────────────────────

    def get_albums(self):
        data = self._get(f"/Users/{self.user_id}/Items",
                         {"IncludeItemTypes": "MusicAlbum", "Recursive": "true",
                          "Fields": "Name,Artists,ProductionYear,ProviderIds"})
        return data.get("Items", [])

    def get_artists(self):
        data = self._get("/Artists",
                         {"UserId": self.user_id, "Fields": "Name,Genres"})
        return data.get("Items", [])

    # ── search ────────────────────────────────────────────────────────────────

    def search_audio(self, query, limit=10):
        data = self._get(f"/Users/{self.user_id}/Items",
                         {"SearchTerm": query, "IncludeItemTypes": "Audio",
                          "Recursive": "true", "Limit": limit,
                          "Fields": "Name,Artists,Album,ProductionYear,ProviderIds"})
        return data.get("Items", [])

    # ── conversion ────────────────────────────────────────────────────────────

    def item_to_songs_dict(self, item):
        artists = item.get("Artists") or ([item.get("AlbumArtist")] if item.get("AlbumArtist") else [])
        d = {"title": item.get("Name", ""), "id": {self.ID_KEY: item["Id"]}}
        if artists:
            d["artists"] = [a for a in artists if a]
        if item.get("Album"):
            d["album"] = item["Album"]
        if item.get("ProductionYear"):
            d["date"] = str(item["ProductionYear"])
        pids = item.get("ProviderIds", {})
        isrc = pids.get("isrc") or pids.get("Isrc")
        if isrc:
            d["isrc"] = isrc
        return {k: v for k, v in d.items() if v}

    def album_to_dict(self, item):
        artists = item.get("Artists") or ([item.get("AlbumArtist")] if item.get("AlbumArtist") else [])
        d = {"name": item.get("Name", ""), "id": {self.ID_KEY: item["Id"]}}
        if artists:
            d["artists"] = [a for a in artists if a]
        if item.get("ProductionYear"):
            d["date"] = str(item["ProductionYear"])
        return {k: v for k, v in d.items() if v}

    def artist_to_dict(self, item):
        d = {"name": item.get("Name", ""), "id": {self.ID_KEY: item["Id"]}}
        if item.get("Genres"):
            d["genres"] = item["Genres"]
        return {k: v for k, v in d.items() if v}

    # ── vault-aware match ─────────────────────────────────────────────────────

    def resolve_track(self, song, fuzzy_ratio):
        """Find Jellyfin item ID for song. Flips orphan→linked on success."""
        try:
            return song["id"][self.ID_KEY]
        except KeyError:
            pass

        for plat, pid in (song.get("id") or {}).items():
            if plat in ("vault", self.ID_KEY) or not pid:
                continue
            learned = matchings.lookup(plat, str(pid), self.ID_KEY)
            if learned:
                return learned

        if song.get("isrc"):
            learned = matchings.lookup_by_isrc(song["isrc"], self.ID_KEY)
            if learned:
                return learned

        q = " ".join(filter(None, [song.get("title"), (song.get("artists") or [""])[0]]))
        if not q:
            return None

        results = self.search_audio(q, limit=10)
        best_score, best_id = 0, None
        for r in results:
            score = fuzzymatch.similarity(song, self.item_to_songs_dict(r))
            if score and score > best_score:
                best_score, best_id = score, r.get("Id")

        if best_score >= fuzzy_ratio and best_id:
            for plat, pid in (song.get("id") or {}).items():
                if plat not in ("vault", self.ID_KEY) and pid:
                    matchings.save(plat, str(pid), self.ID_KEY, best_id,
                                   src_isrc=song.get("isrc"),
                                   src_title=song.get("title"),
                                   src_artist="; ".join(song.get("artists") or []))
            vault_cid = (song.get("id") or {}).get("vault")
            if vault_cid:
                try:
                    from ultrasonics.tools import vault as _vault
                    tl = _vault.get_link(vault_cid, self.ID_KEY)
                    if tl and tl["status"] == "orphan":
                        _vault.flip_orphan_to_linked(vault_cid, self.ID_KEY, best_id)
                except Exception as ve:
                    log.debug(f"vault flip skipped: {ve}")
            return best_id
        return None


def run(settings_dict, **kwargs):
    database   = kwargs["database"]
    component  = kwargs["component"]
    applet_id  = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    server_url = database.get("server_url", "").strip()
    username   = database.get("username",   "").strip()
    password   = database.get("password",   "")

    if not all([server_url, username, password]):
        raise Exception("Jellyfin: server URL, username, and password are required.")

    api = JellyfinAPI(server_url, username, password)
    try:
        api.authenticate()
    except Exception as e:
        raise Exception(f"Jellyfin authentication failed: {e}")

    sync_mode   = database.get("sync_mode", "playlists")
    fuzzy_ratio = _parse_ratio(settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio"))

    if sync_mode == "favorites":
        if component == "inputs":
            return [{"name": "Favorites",
                     "id": {api.ID_KEY: "__favorites__"},
                     "songs": [api.item_to_songs_dict(f) for f in api.get_favorites()]}]
        existing_ids = {f["Id"] for f in api.get_favorites()}
        for pl in songs_dict:
            for song in pl.get("songs", []):
                try:
                    mid = api.resolve_track(song, fuzzy_ratio)
                    if mid and mid not in existing_ids:
                        api.set_favorite(mid, True)
                        existing_ids.add(mid)
                except Exception as e:
                    log.warning(f"Jellyfin favorite failed for '{song.get('title')}': {e}")

    elif sync_mode == "albums":
        if component == "inputs":
            return [{"name": "Albums",
                     "id": {api.ID_KEY: "__albums__"},
                     "songs": [api.album_to_dict(a) for a in api.get_albums()]}]
        log.info("Jellyfin albums output: library is read-only — no write action taken.")

    elif sync_mode == "artists":
        if component == "inputs":
            return [{"name": "Artists",
                     "id": {api.ID_KEY: "__artists__"},
                     "songs": [api.artist_to_dict(a) for a in api.get_artists()]}]
        log.info("Jellyfin artists output: library is read-only — no write action taken.")

    else:
        # playlists
        if component == "inputs":
            playlists = api.get_playlists()
            result = [{"name": pl.get("Name", ""), "id": {api.ID_KEY: pl["Id"]}}
                      for pl in playlists]
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching Jellyfin playlists"):
                items = api.get_playlist_items(pl["id"][api.ID_KEY])
                result[i]["songs"] = [api.item_to_songs_dict(it) for it in items]
            return result

        existing_pls = {pl.get("Name", ""): pl["Id"] for pl in api.get_playlists()}
        for playlist in songs_dict:
            name  = playlist.get("name", "Untitled")
            pl_id = existing_pls.get(name)
            if not pl_id:
                try:
                    pl_id = api.create_playlist(name)
                except Exception as e:
                    log.error(f"Could not create Jellyfin playlist '{name}': {e}")
                    continue

            existing_items  = api.get_playlist_items(pl_id)
            existing_tracks = [api.item_to_songs_dict(it) for it in existing_items]
            existing_ids    = [it.get("Id") for it in existing_items]

            to_add = []
            for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                try:
                    if fuzzymatch.duplicate(song, existing_tracks, fuzzy_ratio):
                        continue
                    mid = api.resolve_track(song, fuzzy_ratio)
                    if mid and mid not in existing_ids:
                        to_add.append(mid)
                except Exception as e:
                    log.warning(f"Jellyfin match failed for '{song.get('title')}': {e}")

            api.playlist_add_items(pl_id, to_add)


def test(database, **kwargs):
    server_url = database.get("server_url", "").strip()
    username   = database.get("username",   "").strip()
    password   = database.get("password",   "")
    if not all([server_url, username, password]):
        raise Exception("Server URL, username, and password are required.")
    api = JellyfinAPI(server_url, username, password)
    api.authenticate()
    log.info(f"Jellyfin connected: {server_url}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch from Jellyfin."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": "Write to Jellyfin."},
        {"type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
         "id": "existing_playlists", "options": ["Append", "Update"], "required": True},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
