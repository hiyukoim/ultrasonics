#!/usr/bin/env python3

"""
up_apple music

Input and output plugin for Apple Music / iCloud Music Library.
Uses the MusicKit JS OAuth flow (developer token + user music token).

Prerequisites
-------------
1. Apple Developer Program membership ($99/year).
2. Create a MusicKit key in the Apple Developer portal.
3. Note your Team ID, Key ID, and download the .p8 private key file.
4. Paste the p8 contents, Team ID, and Key ID into the plugin settings.
5. To authorise a user, visit /apple_music/auth in the ultrasonics web UI.

Auth flow
---------
A short-lived developer JWT (ES256, 6-month max) is used as the bearer token
for all Apple Music catalog requests. For user library reads and writes a
separate user token is obtained via MusicKit JS in the browser.
The user token is stored (per-applet) in the plugin database.

Install
-------
pip install PyJWT cryptography

API
---
Base URL: https://api.music.apple.com/v1/
Rate limit: 3000 req/hour per user token.
ISRC is available in song objects (.attributes.isrc) — high match quality.
Playlist track limit: effectively unlimited for user library.
"""

import json
import time
from urllib.parse import urlencode

from ultrasonics import logs
from ultrasonics.tools import matchings, name_filter
from ultrasonics.tools.resolver import resolve_track
from ultrasonics.tools import http_retry

log = logs.create_log(__name__)

_AVAILABLE = True
try:
    import jwt
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
except ImportError:
    _AVAILABLE = False
    log.warning("PyJWT or cryptography not installed — up_apple music unavailable. "
                "pip install PyJWT cryptography")

handshake = {
    "name": "apple music",
    "description": "sync playlists and library to/from apple music (musickit — developer account required)",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites"],
    "version": "0.2",
    "settings": [
        {
            "type": "string",
            "value": (
                "Apple Music requires an Apple Developer account ($99/yr). "
                "Create a MusicKit key in the Apple Developer portal and paste "
                "your credentials below. After saving, visit /apple_music/auth "
                "to authorise your Apple Music account."
            ),
        },
        {"type": "text",     "label": "Team ID",              "name": "team_id",       "value": ""},
        {"type": "text",     "label": "Key ID",               "name": "key_id",        "value": ""},
        {"type": "textarea", "label": "Private Key (.p8 PEM)", "name": "private_key_pem", "value": ""},
        {
            "type": "auth",
            "label": "Authorise Apple Music",
            "path": "/apple_music/auth/request",
        },
        {
            "type": "text",
            "label": "User Music Token (auto-filled after auth)",
            "name": "user_music_token",
            "value": "",
        },
        {
            "type": "radio",
            "label": "Sync Mode",
            "name": "sync_mode",
            "id": "sync_mode",
            "options": ["playlists", "favorites"],
        },
        {"type": "text", "label": "Storefront (e.g. us, gb)", "name": "storefront", "value": "us"},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": "85"},
    ],
}

_BASE = "https://api.music.apple.com/v1"
_ID_KEY = "apple music"
_DEV_TOKEN_TTL = 15552000  # 6 months in seconds


def _parse_ratio(val, default=85.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _build_dev_token(team_id, key_id, private_key_pem):
    """Generate a short-lived ES256 developer JWT for the MusicKit API."""
    if not _AVAILABLE:
        raise Exception("PyJWT and cryptography are required. pip install PyJWT cryptography")
    private_key = load_pem_private_key(private_key_pem.encode(), password=None)
    now = int(time.time())
    payload = {
        "iss": team_id,
        "iat": now,
        "exp": now + _DEV_TOKEN_TTL,
    }
    token = jwt.encode(payload, private_key, algorithm="ES256",
                       headers={"kid": key_id})
    return token if isinstance(token, str) else token.decode()


class AppleMusicAPI:
    """
    Thin wrapper around the Apple Music REST API.
    All methods take/return plain dicts.
    """

    def __init__(self, dev_token, user_music_token, storefront="us"):
        self.storefront = storefront.lower()
        self._headers = {
            "Authorization": f"Bearer {dev_token}",
            "Music-User-Token": user_music_token,
        }

    def _get(self, path, params=None):
        resp = http_retry.get(f"{_BASE}{path}", headers=self._headers, params=params or {})
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, body):
        resp = http_retry.post(f"{_BASE}{path}", headers=self._headers, json=body)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    def _delete(self, path):
        resp = http_retry.delete(f"{_BASE}{path}", headers=self._headers)
        resp.raise_for_status()

    # ── library playlists ─────────────────────────────────────────────────────

    def get_library_playlists(self):
        """Return list of user library playlist resource dicts."""
        results = []
        path = "/me/library/playlists"
        while path:
            data = self._get(path)
            results.extend(data.get("data", []))
            path = data.get("next")
        return results

    def get_playlist_tracks(self, playlist_id):
        """Return list of track resource dicts from a library playlist."""
        results = []
        path = f"/me/library/playlists/{playlist_id}/tracks"
        while path:
            data = self._get(path)
            results.extend(data.get("data", []))
            path = data.get("next")
        return results

    def create_playlist(self, name, description=""):
        body = {"attributes": {"name": name, "description": description}}
        data = self._post("/me/library/playlists", body)
        return data.get("data", [{}])[0].get("id")

    def add_tracks_to_playlist(self, playlist_id, track_ids):
        """track_ids: list of Apple Music catalog IDs."""
        body = {"data": [{"id": tid, "type": "songs"} for tid in track_ids]}
        self._post(f"/me/library/playlists/{playlist_id}/tracks", body)

    # ── catalog search ────────────────────────────────────────────────────────

    def search_catalog(self, query, storefront=None):
        """Search catalog for songs. Returns list of song resource dicts."""
        sf = storefront or self.storefront
        params = {"term": query, "types": "songs", "limit": "10"}
        data = self._get(f"/catalog/{sf}/search", params=params)
        return data.get("results", {}).get("songs", {}).get("data", [])

    def get_catalog_by_isrc(self, isrc, storefront=None):
        """Look up a catalog song by ISRC. Returns list of song resource dicts."""
        sf = storefront or self.storefront
        params = {"filter[isrc]": isrc}
        data = self._get(f"/catalog/{sf}/songs", params=params)
        return data.get("data", [])

    # ── library (heavy metal / favorites) ─────────────────────────────────────

    def get_library_songs(self):
        results = []
        path = "/me/library/songs"
        while path:
            data = self._get(path)
            results.extend(data.get("data", []))
            path = data.get("next")
        return results

    def add_to_library(self, catalog_ids):
        """Add songs by catalog ID to the user's library/favorites."""
        params = {"ids[songs]": ",".join(catalog_ids)}
        resp = http_retry.post(
            f"{_BASE}/me/library",
            headers=self._headers,
            params=params,
        )
        resp.raise_for_status()


def _resource_to_songs_dict(resource):
    """Convert an Apple Music song resource to ultrasonics songs_dict item."""
    attr = resource.get("attributes", {})
    catalog_id = resource.get("id", "")
    song = {
        "title":   attr.get("name", ""),
        "artists": [attr.get("artistName", "")] if attr.get("artistName") else [],
        "album":   attr.get("albumName", ""),
    }
    if attr.get("releaseDate"):
        song["date"] = attr["releaseDate"][:4]
    if attr.get("isrc"):
        song["isrc"] = attr["isrc"]
    if attr.get("artwork", {}).get("url"):
        url_tpl = attr["artwork"]["url"]
        song["image"] = url_tpl.replace("{w}", "300").replace("{h}", "300")
    if catalog_id:
        song.setdefault("id", {})["apple music"] = catalog_id
    return song


def run(settings_dict, **kwargs):
    database    = kwargs["database"]
    component   = kwargs["component"]
    applet_id   = kwargs["applet_id"]
    songs_dict  = kwargs["songs_dict"]

    if not _AVAILABLE:
        raise Exception("PyJWT and cryptography are required. pip install PyJWT cryptography")

    team_id          = database.get("team_id",           "").strip()
    key_id           = database.get("key_id",            "").strip()
    private_key_pem  = database.get("private_key_pem",   "").strip()
    user_music_token = database.get("user_music_token",  "").strip()
    storefront       = database.get("storefront",        "us").strip() or "us"
    sync_mode        = database.get("sync_mode",         "playlists").lower()
    fuzzy_ratio      = _parse_ratio(settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio"))

    if not all([team_id, key_id, private_key_pem]):
        raise Exception("Apple Music: Team ID, Key ID, and Private Key are required.")
    if not user_music_token:
        raise Exception(
            "Apple Music: User Music Token is missing. "
            "Visit /apple_music/auth in the web UI to authorise your account."
        )

    dev_token = _build_dev_token(team_id, key_id, private_key_pem)
    api = AppleMusicAPI(dev_token, user_music_token, storefront)

    # ── FAVORITES ────────────────────────────────────────────────────────────

    if sync_mode == "favorites":
        if component == "inputs":
            songs = [_resource_to_songs_dict(r) for r in api.get_library_songs()]
            return [{"name": "Apple Music Library", "id": {"apple music": "__library__"}, "songs": songs}]

        for playlist in songs_dict:
            catalog_ids = []
            for song in playlist.get("songs", []):
                cid = _resolve_catalog_id(song, api, storefront, fuzzy_ratio)
                if cid:
                    catalog_ids.append(cid)
            if catalog_ids:
                for i in range(0, len(catalog_ids), 200):
                    api.add_to_library(catalog_ids[i:i + 200])
                    time.sleep(0.5)
        return

    # ── PLAYLISTS ────────────────────────────────────────────────────────────

    if component == "inputs":
        output = []
        for pl_resource in api.get_library_playlists():
            pl_attr   = pl_resource.get("attributes", {})
            pl_id     = pl_resource.get("id", "")
            pl_name   = pl_attr.get("name", "Untitled")
            tracks    = api.get_playlist_tracks(pl_id)
            songs     = [_resource_to_songs_dict(r) for r in tracks]
            output.append({
                "name": pl_name,
                "id": {"apple music": pl_id},
                "songs": songs,
            })
        return output

    # component == "outputs"
    for playlist in songs_dict:
        pl_name = name_filter.filter(playlist.get("name", "Untitled"))
        # Find or create playlist
        existing = {
            p.get("attributes", {}).get("name", "").lower(): p.get("id")
            for p in api.get_library_playlists()
        }
        pl_id = existing.get(pl_name.lower())
        if not pl_id:
            pl_id = api.create_playlist(pl_name)

        if not pl_id:
            log.error(f"Apple Music: could not create playlist '{pl_name}'")
            continue

        existing_tracks = {
            r.get("attributes", {}).get("isrc")
            for r in api.get_playlist_tracks(pl_id)
            if r.get("attributes", {}).get("isrc")
        }

        new_ids = []
        for song in playlist.get("songs", []):
            # Skip if already present by ISRC
            if song.get("isrc") and song["isrc"] in existing_tracks:
                continue
            cid = _resolve_catalog_id(song, api, storefront, fuzzy_ratio)
            if cid:
                new_ids.append(cid)

        if new_ids:
            for i in range(0, len(new_ids), 200):
                api.add_tracks_to_playlist(pl_id, new_ids[i:i + 200])
                time.sleep(0.5)
            log.info(f"Apple Music: added {len(new_ids)} tracks to '{pl_name}'")


def _resolve_catalog_id(song, api, storefront, fuzzy_ratio):
    """
    Try ISRC first, then title+artist search with fuzzy match fallback.
    Returns catalog song ID string, or None.
    """
    from ultrasonics.tools import fuzzymatch

    # 1. Already know the ID
    if song.get("id", {}).get("apple music"):
        return song["id"]["apple music"]

    # 2. ISRC lookup
    if song.get("isrc"):
        results = api.get_catalog_by_isrc(song["isrc"], storefront)
        if results:
            cid = results[0]["id"]
            from ultrasonics.tools import vault as _vault
            if song.get("id", {}).get("vault"):
                _vault.flip_orphan_to_linked(song["id"]["vault"], "apple music", cid)
            return cid

    # 3. Search + fuzzy match
    artists = song.get("artists", [])
    artist_str = artists[0] if artists else ""
    query = f"{song.get('title', '')} {artist_str}".strip()
    if not query:
        return None

    results = api.search_catalog(query, storefront)
    candidates = [_resource_to_songs_dict(r) for r in results]
    for cand in candidates:
        score = fuzzymatch.similarity(song, cand)
        if score >= fuzzy_ratio:
            cid = cand.get("id", {}).get("apple music")
            if cid:
                from ultrasonics.tools import vault as _vault
                if song.get("id", {}).get("vault"):
                    _vault.flip_orphan_to_linked(song["id"]["vault"], "apple music", cid)
            return cid

    return None


def builder(**kwargs):
    return []


def test(database, **kwargs):
    if not _AVAILABLE:
        log.error("PyJWT / cryptography not installed.")
        return False
    try:
        team_id         = database.get("team_id",         "").strip()
        key_id          = database.get("key_id",          "").strip()
        private_key_pem = database.get("private_key_pem", "").strip()
        user_music_token= database.get("user_music_token","").strip()
        storefront      = database.get("storefront",      "us").strip() or "us"
        if not all([team_id, key_id, private_key_pem, user_music_token]):
            log.error("Apple Music: missing credentials for test.")
            return False
        dev_token = _build_dev_token(team_id, key_id, private_key_pem)
        api = AppleMusicAPI(dev_token, user_music_token, storefront)
        playlists = api.get_library_playlists()
        log.info(f"Apple Music test: found {len(playlists)} playlists.")
        return True
    except Exception as exc:
        log.error(f"Apple Music test failed: {exc}")
        return False
