#!/usr/bin/env python3

"""
up_tidal

Input and output plugin for TIDAL.
Auth: OAuth2 Bearer token (access_token + optional refresh).
Vault contract: flips orphan→linked on successful match.
Failures are isolated per-song.
"""

import time

import requests
from tqdm import tqdm

from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter

log = logs.create_log(__name__)

handshake = {
    "name": "tidal",
    "description": "sync playlists, favorites, albums, and artists to/from tidal",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.2",
    "settings": [
        {"type": "string",
         "value": "TIDAL uses OAuth2. Paste your access token and user ID. Provide a refresh token + client credentials for automatic renewal."},
        {"type": "text", "label": "Access Token",              "name": "access_token",  "value": ""},
        {"type": "text", "label": "Refresh Token (optional)",  "name": "refresh_token", "value": ""},
        {"type": "text", "label": "Client ID (for refresh)",   "name": "client_id",     "value": ""},
        {"type": "text", "label": "Client Secret (for refresh)","name": "client_secret","value": ""},
        {"type": "text", "label": "User ID",                   "name": "user_id",       "value": ""},
        {"type": "text", "label": "Country Code",              "name": "country_code",  "value": "US"},
        {"type": "select", "label": "Sync Mode", "name": "sync_mode",
         "options": ["playlists", "favorites", "albums", "artists"], "value": "playlists"},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": "85"},
    ],
}

_BASE      = "https://api.tidal.com/v1"
_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"


def _parse_ratio(val, default=85.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class TidalAPI:
    ID_KEY = "tidal"

    def __init__(self, access_token, user_id, country="US",
                 refresh_token=None, client_id=None, client_secret=None):
        self.access_token  = access_token
        self.user_id       = str(user_id)
        self.country       = country
        self.refresh_token = refresh_token
        self.client_id     = client_id
        self.client_secret = client_secret

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}"}

    def _refresh(self):
        if not all([self.refresh_token, self.client_id, self.client_secret]):
            raise Exception("Cannot refresh TIDAL token: missing client credentials.")
        resp = requests.post(
            _TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            auth=(self.client_id, self.client_secret), timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]

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
        resp = requests.post(f"{_BASE}{path}", headers=self._headers(),
                             json=data or {}, params=p, timeout=30)
        if resp.status_code == 401 and self.refresh_token:
            self._refresh()
            resp = requests.post(f"{_BASE}{path}", headers=self._headers(),
                                 json=data or {}, params=p, timeout=30)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    # ── playlists ─────────────────────────────────────────────────────────────

    def get_playlists(self):
        return self._get(f"/users/{self.user_id}/playlists", {"limit": 50}).get("items", [])

    def get_playlist_tracks(self, uuid):
        return self._get(f"/playlists/{uuid}/tracks", {"limit": 10000}).get("items", [])

    def create_playlist(self, name):
        data = self._post(f"/users/{self.user_id}/playlists",
                          data={"title": name, "description": ""})
        return data.get("uuid") or data.get("id")

    def playlist_add_tracks(self, uuid, track_ids):
        for i in range(0, len(track_ids), 100):
            self._post(f"/playlists/{uuid}/tracks",
                       data={"trackIds": track_ids[i:i + 100], "toIndex": 0})

    # ── favorites ─────────────────────────────────────────────────────────────

    def get_favorite_tracks(self):
        return [item.get("item", item)
                for item in self._get(f"/users/{self.user_id}/favorites/tracks",
                                      {"limit": 10000}).get("items", [])]

    def add_favorite_track(self, track_id):
        self._post(f"/users/{self.user_id}/favorites/tracks",
                   data={"trackId": int(track_id)})

    def get_favorite_albums(self):
        return [item.get("item", item)
                for item in self._get(f"/users/{self.user_id}/favorites/albums",
                                      {"limit": 10000}).get("items", [])]

    def add_favorite_album(self, album_id):
        self._post(f"/users/{self.user_id}/favorites/albums",
                   data={"albumId": int(album_id)})

    def get_favorite_artists(self):
        return [item.get("item", item)
                for item in self._get(f"/users/{self.user_id}/favorites/artists",
                                      {"limit": 10000}).get("items", [])]

    def add_favorite_artist(self, artist_id):
        self._post(f"/users/{self.user_id}/favorites/artists",
                   data={"artistId": int(artist_id)})

    # ── search ────────────────────────────────────────────────────────────────

    def search_tracks(self, query, limit=10):
        return self._get("/search", {"query": query, "types": "TRACKS", "limit": limit}
                         ).get("tracks", {}).get("items", [])

    def search_albums(self, query, limit=10):
        return self._get("/search", {"query": query, "types": "ALBUMS", "limit": limit}
                         ).get("albums", {}).get("items", [])

    def search_artists(self, query, limit=10):
        return self._get("/search", {"query": query, "types": "ARTISTS", "limit": limit}
                         ).get("artists", {}).get("items", [])

    # ── conversion ────────────────────────────────────────────────────────────

    def track_to_songs_dict(self, t):
        artists = [a["name"] for a in t.get("artists", [])] or \
                  ([t["artist"]["name"]] if t.get("artist") else [])
        d = {"title": t.get("title", ""), "id": {self.ID_KEY: str(t["id"])}}
        if artists:
            d["artists"] = artists
        if t.get("album", {}).get("title"):
            d["album"] = t["album"]["title"]
        if t.get("releaseDate"):
            d["date"] = t["releaseDate"][:4]
        if t.get("isrc"):
            d["isrc"] = t["isrc"]
        return {k: v for k, v in d.items() if v}

    def album_to_dict(self, a):
        artists = [x["name"] for x in a.get("artists", [])] or \
                  ([a["artist"]["name"]] if a.get("artist") else [])
        d = {"name": a.get("title", ""), "id": {self.ID_KEY: str(a["id"])}}
        if artists:
            d["artists"] = artists
        if a.get("releaseDate"):
            d["date"] = a["releaseDate"][:4]
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

    access_token = database.get("access_token", "").strip()
    user_id      = database.get("user_id",       "").strip()
    if not access_token or not user_id:
        raise Exception("TIDAL: Access Token and User ID are required.")

    api = TidalAPI(
        access_token=access_token,
        user_id=user_id,
        country=(database.get("country_code") or "US").strip(),
        refresh_token=database.get("refresh_token", "").strip() or None,
        client_id=database.get("client_id",     "").strip() or None,
        client_secret=database.get("client_secret", "").strip() or None,
    )

    sync_mode   = database.get("sync_mode", "playlists")
    fuzzy_ratio = _parse_ratio(settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio"))

    if sync_mode == "favorites":
        if component == "inputs":
            return [{"name": "Favorites",
                     "id": {api.ID_KEY: "__favorites__"},
                     "songs": [api.track_to_songs_dict(t) for t in api.get_favorite_tracks()]}]
        existing = {str(t.get("id")) for t in api.get_favorite_tracks()}
        for pl in songs_dict:
            for song in pl.get("songs", []):
                try:
                    mid = api.resolve_track(song, fuzzy_ratio)
                    if mid and str(mid) not in existing:
                        api.add_favorite_track(mid)
                        existing.add(str(mid))
                except Exception as e:
                    log.warning(f"TIDAL favorite failed for '{song.get('title')}': {e}")

    elif sync_mode == "albums":
        if component == "inputs":
            return [{"name": "Albums",
                     "id": {api.ID_KEY: "__albums__"},
                     "songs": [api.album_to_dict(a) for a in api.get_favorite_albums()]}]
        existing = {str(a.get("id")) for a in api.get_favorite_albums()}
        for pl in songs_dict:
            for alb in pl.get("songs", []):
                try:
                    alb_id = alb.get("id", {}).get(api.ID_KEY)
                    if not alb_id:
                        upc = alb.get("upc")
                        if upc:
                            res = api.search_albums(f"upc:{upc}", limit=1)
                            if res:
                                alb_id = str(res[0]["id"])
                        if not alb_id:
                            name   = alb.get("name", "")
                            artist = (alb.get("artists") or [""])[0]
                            res    = api.search_albums(f"{artist} {name}".strip(), limit=5)
                            if res:
                                alb_id = str(res[0]["id"])
                    if alb_id and alb_id not in existing:
                        api.add_favorite_album(alb_id)
                        existing.add(alb_id)
                except Exception as e:
                    log.warning(f"TIDAL album add failed for '{alb.get('name')}': {e}")

    elif sync_mode == "artists":
        if component == "inputs":
            return [{"name": "Artists",
                     "id": {api.ID_KEY: "__artists__"},
                     "songs": [api.artist_to_dict(a) for a in api.get_favorite_artists()]}]
        existing = {str(a.get("id")) for a in api.get_favorite_artists()}
        for pl in songs_dict:
            for art in pl.get("songs", []):
                try:
                    art_id = art.get("id", {}).get(api.ID_KEY)
                    if not art_id:
                        res = api.search_artists(art.get("name", ""), limit=1)
                        if res:
                            art_id = str(res[0]["id"])
                    if art_id and art_id not in existing:
                        api.add_favorite_artist(art_id)
                        existing.add(art_id)
                except Exception as e:
                    log.warning(f"TIDAL artist follow failed for '{art.get('name')}': {e}")

    else:
        # playlists
        if component == "inputs":
            playlists = api.get_playlists()
            result = [{"name": pl.get("title", "Untitled"),
                       "id": {api.ID_KEY: pl.get("uuid") or pl.get("id")}}
                      for pl in playlists]
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching TIDAL playlists"):
                tracks = api.get_playlist_tracks(pl["id"][api.ID_KEY])
                result[i]["songs"] = [api.track_to_songs_dict(t) for t in tracks]
            return result

        existing_pls = {pl.get("title", ""): (pl.get("uuid") or pl.get("id"))
                        for pl in api.get_playlists()}
        for playlist in songs_dict:
            name  = playlist.get("name", "Untitled")
            pl_id = existing_pls.get(name)
            if not pl_id:
                try:
                    pl_id = api.create_playlist(name)
                except Exception as e:
                    log.error(f"Could not create TIDAL playlist '{name}': {e}")
                    continue

            existing_tracks = api.get_playlist_tracks(pl_id)
            existing_ids    = {str(t.get("id")) for t in existing_tracks}
            to_add = []
            for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                try:
                    mid = api.resolve_track(song, fuzzy_ratio)
                    if mid and str(mid) not in existing_ids:
                        to_add.append(int(mid))
                except Exception as e:
                    log.warning(f"TIDAL match failed for '{song.get('title')}': {e}")
            if to_add:
                api.playlist_add_tracks(pl_id, to_add)


def test(database, **kwargs):
    access_token = database.get("access_token", "").strip()
    user_id      = database.get("user_id",       "").strip()
    if not access_token or not user_id:
        raise Exception("Access Token and User ID are required.")
    api = TidalAPI(access_token=access_token, user_id=user_id,
                   country=database.get("country_code", "US").strip())
    api._get(f"/users/{user_id}")
    log.info(f"TIDAL connected for user {user_id}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch from TIDAL."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": "Write to TIDAL."},
        {"type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
         "id": "existing_playlists", "options": ["Append", "Update"], "required": True},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
