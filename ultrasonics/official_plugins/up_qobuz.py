#!/usr/bin/env python3

"""
up_qobuz

Input and output plugin for Qobuz.
Auth: app_id + app_secret + MD5(password) login → user_auth_token.
Vault contract: flips orphan→linked on successful match.
Max 1999 tracks per playlist. Failures are isolated per-song.
"""

import hashlib
import time

import requests
from tqdm import tqdm

from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter

log = logs.create_log(__name__)

handshake = {
    "name": "qobuz",
    "description": "sync playlists, favorites, albums, and artists to/from qobuz (max 1999 tracks/playlist)",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.2",
    "settings": [
        {"type": "string", "value": "Qobuz requires an app_id, app_secret, and your account credentials."},
        {"type": "text", "label": "App ID",           "name": "app_id",     "value": ""},
        {"type": "text", "label": "App Secret",       "name": "app_secret", "value": ""},
        {"type": "text", "label": "Email / Username", "name": "username",   "value": ""},
        {"type": "text", "label": "Password",         "name": "password",   "value": ""},
        {"type": "select", "label": "Sync Mode", "name": "sync_mode",
         "options": ["playlists", "favorites", "albums", "artists"], "value": "playlists"},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": "85"},
    ],
}

_BASE = "https://www.qobuz.com/api.json/0.2"
_MAX_TRACKS = 1999


def _parse_ratio(val, default=85.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class QobuzAPI:
    ID_KEY = "qobuz"

    def __init__(self, app_id, app_secret, username, password):
        self.app_id          = app_id
        self.app_secret      = app_secret
        self.username        = username
        self.password        = password
        self.user_auth_token = None

    def login(self):
        pwd_md5 = hashlib.md5(self.password.encode()).hexdigest()
        resp = requests.get(
            f"{_BASE}/user/login",
            params={"app_id": self.app_id, "username": self.username,
                    "password": pwd_md5, "email": self.username},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            raise Exception(f"Qobuz login error: {data.get('message')}")
        self.user_auth_token = data["user_auth_token"]

    def _auth(self):
        return {"app_id": self.app_id, "user_auth_token": self.user_auth_token}

    def _get(self, path, params=None):
        p = dict(self._auth())
        if params:
            p.update(params)
        resp = requests.get(f"{_BASE}{path}", params=p, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            raise Exception(f"Qobuz API error: {data.get('message')}")
        return data

    def _post(self, path, data=None):
        payload = dict(self._auth())
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

    # ── playlists ─────────────────────────────────────────────────────────────

    def get_playlists(self):
        return self._get("/playlist/getUserPlaylists",
                         {"limit": 500, "offset": 0}).get("playlists", {}).get("items", [])

    def get_playlist_tracks(self, playlist_id):
        all_tracks, offset = [], 0
        while True:
            data   = self._get("/playlist/get",
                               {"playlist_id": playlist_id, "extra": "tracks",
                                "limit": 50, "offset": offset})
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
        for i in range(0, len(track_ids), 50):
            self._post("/playlist/addTracks", {
                "playlist_id": playlist_id,
                "track_ids": ",".join(str(tid) for tid in track_ids[i:i + 50]),
            })

    # ── favorites ─────────────────────────────────────────────────────────────

    def get_favorites(self, item_type="tracks"):
        all_items, offset = [], 0
        while True:
            data  = self._get("/favorite/getUserFavorites",
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

    # ── search ────────────────────────────────────────────────────────────────

    def search_tracks(self, query, limit=10):
        return self._get("/catalog/search",
                         {"query": query, "type": "tracks", "limit": limit}
                         ).get("tracks", {}).get("items", [])

    def search_albums(self, query, limit=10):
        return self._get("/catalog/search",
                         {"query": query, "type": "albums", "limit": limit}
                         ).get("albums", {}).get("items", [])

    def search_artists(self, query, limit=10):
        return self._get("/catalog/search",
                         {"query": query, "type": "artists", "limit": limit}
                         ).get("artists", {}).get("items", [])

    # ── conversion ────────────────────────────────────────────────────────────

    def track_to_songs_dict(self, t):
        performer = t.get("performer") or t.get("composer") or {}
        d = {"title": t.get("title", ""), "id": {self.ID_KEY: str(t["id"])}}
        if performer.get("name"):
            d["artists"] = [performer["name"]]
        if t.get("album", {}).get("title"):
            d["album"] = t["album"]["title"]
        if t.get("album", {}).get("release_date_original"):
            d["date"] = t["album"]["release_date_original"][:4]
        if t.get("isrc"):
            d["isrc"] = t["isrc"]
        return {k: v for k, v in d.items() if v}

    def album_to_dict(self, a):
        d = {"name": a.get("title", ""), "id": {self.ID_KEY: str(a["id"])}}
        if a.get("artist", {}).get("name"):
            d["artists"] = [a["artist"]["name"]]
        if a.get("release_date_original"):
            d["date"] = a["release_date_original"][:4]
        if a.get("upc"):
            d["upc"] = a["upc"]
        return {k: v for k, v in d.items() if v}

    def artist_to_dict(self, a):
        return {"name": a.get("name", ""), "id": {self.ID_KEY: str(a["id"])}}

    # ── vault-aware match ─────────────────────────────────────────────────────

    def resolve_track(self, song, fuzzy_ratio):
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
        results = self.search_tracks(q, limit=10)
        best_score, best_id = 0, None
        for r in results:
            score = fuzzymatch.similarity(song, self.track_to_songs_dict(r))
            if score and score > best_score:
                best_score, best_id = score, r.get("id")

        if best_score >= fuzzy_ratio and best_id:
            best_id = str(best_id)
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

    app_id     = database.get("app_id",     "").strip()
    app_secret = database.get("app_secret", "").strip()
    username   = database.get("username",   "").strip()
    password   = database.get("password",   "")

    if not all([app_id, app_secret, username, password]):
        raise Exception("Qobuz: app_id, app_secret, username, and password are required.")

    api = QobuzAPI(app_id, app_secret, username, password)
    try:
        api.login()
    except Exception as e:
        raise Exception(f"Qobuz login failed: {e}")

    sync_mode   = database.get("sync_mode", "playlists")
    fuzzy_ratio = _parse_ratio(settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio"))

    if sync_mode == "favorites":
        if component == "inputs":
            return [{"name": "Favorites",
                     "id": {api.ID_KEY: "__favorites__"},
                     "songs": [api.track_to_songs_dict(t) for t in api.get_favorites("tracks")]}]
        existing = {str(t.get("id")) for t in api.get_favorites("tracks")}
        for pl in songs_dict:
            for song in pl.get("songs", []):
                try:
                    mid = api.resolve_track(song, fuzzy_ratio)
                    if mid and str(mid) not in existing:
                        api.add_favorite(mid, "track")
                        existing.add(str(mid))
                except Exception as e:
                    log.warning(f"Qobuz favorite failed for '{song.get('title')}': {e}")

    elif sync_mode == "albums":
        if component == "inputs":
            return [{"name": "Albums",
                     "id": {api.ID_KEY: "__albums__"},
                     "songs": [api.album_to_dict(a) for a in api.get_favorites("albums")]}]
        existing = {str(a.get("id")) for a in api.get_favorites("albums")}
        for pl in songs_dict:
            for alb in pl.get("songs", []):
                try:
                    alb_id = alb.get("id", {}).get(api.ID_KEY)
                    if not alb_id:
                        upc = alb.get("upc")
                        if upc:
                            res = api.search_albums(upc, limit=1)
                            if res and res[0].get("upc") == upc:
                                alb_id = str(res[0]["id"])
                        if not alb_id:
                            name   = alb.get("name", "")
                            artist = (alb.get("artists") or [""])[0]
                            res    = api.search_albums(f"{artist} {name}".strip(), limit=5)
                            if res:
                                alb_id = str(res[0]["id"])
                    if alb_id and alb_id not in existing:
                        api.add_favorite(alb_id, "album")
                        existing.add(alb_id)
                except Exception as e:
                    log.warning(f"Qobuz album add failed for '{alb.get('name')}': {e}")

    elif sync_mode == "artists":
        if component == "inputs":
            return [{"name": "Artists",
                     "id": {api.ID_KEY: "__artists__"},
                     "songs": [api.artist_to_dict(a) for a in api.get_favorites("artists")]}]
        existing = {str(a.get("id")) for a in api.get_favorites("artists")}
        for pl in songs_dict:
            for art in pl.get("songs", []):
                try:
                    art_id = art.get("id", {}).get(api.ID_KEY)
                    if not art_id:
                        res = api.search_artists(art.get("name", ""), limit=1)
                        if res:
                            art_id = str(res[0]["id"])
                    if art_id and art_id not in existing:
                        api.add_favorite(art_id, "artist")
                        existing.add(art_id)
                except Exception as e:
                    log.warning(f"Qobuz artist follow failed for '{art.get('name')}': {e}")

    else:
        # playlists
        if component == "inputs":
            playlists = api.get_playlists()
            result = [{"name": pl.get("name", "Untitled"),
                       "id": {api.ID_KEY: str(pl["id"])}}
                      for pl in playlists]
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching Qobuz playlists"):
                tracks = api.get_playlist_tracks(pl["id"][api.ID_KEY])
                result[i]["songs"] = [api.track_to_songs_dict(t) for t in tracks]
            return result

        existing_pls = {pl.get("name", ""): str(pl["id"]) for pl in api.get_playlists()}
        for playlist in songs_dict:
            name  = playlist.get("name", "Untitled")
            pl_id = existing_pls.get(name)
            if not pl_id:
                try:
                    pl_id = api.create_playlist(name)
                except Exception as e:
                    log.error(f"Could not create Qobuz playlist '{name}': {e}")
                    continue

            existing_tracks = api.get_playlist_tracks(pl_id)
            existing_ids    = {str(t.get("id")) for t in existing_tracks}
            incoming_songs  = playlist.get("songs", [])

            if len(incoming_songs) > _MAX_TRACKS:
                log.warning(f"Qobuz: '{name}' has {len(incoming_songs)} tracks; "
                            f"truncating to {_MAX_TRACKS}.")
                incoming_songs = incoming_songs[:_MAX_TRACKS]

            to_add = []
            for song in tqdm(incoming_songs, desc=f"Matching '{name}'"):
                try:
                    mid = api.resolve_track(song, fuzzy_ratio)
                    if mid and str(mid) not in existing_ids:
                        to_add.append(mid)
                except Exception as e:
                    log.warning(f"Qobuz match failed for '{song.get('title')}': {e}")
            api.playlist_add_tracks(pl_id, to_add)


def test(database, **kwargs):
    app_id     = database.get("app_id",     "").strip()
    app_secret = database.get("app_secret", "").strip()
    username   = database.get("username",   "").strip()
    password   = database.get("password",   "")
    if not all([app_id, app_secret, username, password]):
        raise Exception("app_id, app_secret, username, and password are required.")
    api = QobuzAPI(app_id, app_secret, username, password)
    api.login()
    log.info(f"Qobuz connected for {username}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch from Qobuz."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": f"Write to Qobuz (max {_MAX_TRACKS} tracks per playlist)."},
        {"type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
         "id": "existing_playlists", "options": ["Append", "Update"], "required": True},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
