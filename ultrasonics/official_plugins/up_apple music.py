#!/usr/bin/env python3

"""
up_apple music

Input and output plugin for Apple Music / iCloud Music Library.
Uses the media-user-token cookie — no Apple Developer account required.

How to get your media-user-token
---------------------------------
1. Open https://music.apple.com in a browser and sign in.
2. Open DevTools (F12) → Application → Cookies → https://music.apple.com
3. Find the cookie named  media-user-token  and copy its value.
4. Paste it into the plugin settings below.

The token typically lasts weeks to months. If requests start returning 401,
fetch a fresh token using the same steps.

API
---
Base URL: https://api.music.apple.com/v1/
Auth:     Music-User-Token header (no developer JWT needed for user-library calls).
          Catalog-only calls (search, ISRC lookup) use the same token as bearer.
ISRC available on song resources → high-quality matching.
"""

import time

from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, name_filter
from ultrasonics.tools import http_retry

log = logs.create_log(__name__)

handshake = {
    "name": "apple music",
    "description": "sync playlists and library to/from apple music (no developer account needed)",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites"],
    "version": "0.3",
    "settings": [
        {
            "type": "string",
            "value": (
                "To get your media-user-token: open music.apple.com, sign in, "
                "then open DevTools (F12) → Application → Cookies → "
                "https://music.apple.com and copy the value of the "
                "media-user-token cookie."
            ),
        },
        {
            "type": "text",
            "label": "media-user-token",
            "name": "media_user_token",
            "value": "",
        },
        {
            "type": "text",
            "label": "Storefront (e.g. us, jp, gb)",
            "name": "storefront",
            "value": "us",
        },
        {
            "type": "radio",
            "label": "Sync Mode",
            "name": "sync_mode",
            "id": "sync_mode",
            "options": ["playlists", "favorites"],
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": "85",
        },
    ],
}

_BASE = "https://api.music.apple.com/v1"


def _parse_ratio(val, default=85.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class AppleMusicAPI:
    """Thin wrapper around the Apple Music REST API using media-user-token only."""

    def __init__(self, media_user_token, storefront="us"):
        self.storefront = storefront.lower()
        self._headers = {
            # media-user-token doubles as bearer for user-library endpoints
            "Authorization": f"Bearer {media_user_token}",
            "Music-User-Token": media_user_token,
            "Origin": "https://music.apple.com",
        }

    def _get(self, path, params=None):
        resp = http_retry.get(f"{_BASE}{path}", headers=self._headers, params=params or {})
        if resp.status_code == 401:
            raise Exception(
                "Apple Music: 401 Unauthorised — your media-user-token has expired. "
                "Fetch a fresh one from DevTools → Application → Cookies on music.apple.com."
            )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, body=None, params=None):
        resp = http_retry.post(
            f"{_BASE}{path}",
            headers=self._headers,
            json=body,
            params=params or {},
        )
        if resp.status_code == 401:
            raise Exception("Apple Music: 401 — media-user-token expired.")
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    # ── library playlists ─────────────────────────────────────────────────────

    def get_library_playlists(self):
        results = []
        path = "/me/library/playlists"
        while path:
            data = self._get(path)
            results.extend(data.get("data", []))
            path = data.get("next")
        return results

    def get_playlist_tracks(self, playlist_id):
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
        return (data.get("data") or [{}])[0].get("id")

    def add_tracks_to_playlist(self, playlist_id, track_ids):
        body = {"data": [{"id": tid, "type": "songs"} for tid in track_ids]}
        self._post(f"/me/library/playlists/{playlist_id}/tracks", body)

    # ── catalog ────────────────────────────────────────────────────────────────

    def search_catalog(self, query, storefront=None):
        sf = storefront or self.storefront
        params = {"term": query, "types": "songs", "limit": "10"}
        data = self._get(f"/catalog/{sf}/search", params=params)
        return data.get("results", {}).get("songs", {}).get("data", [])

    def get_catalog_by_isrc(self, isrc, storefront=None):
        sf = storefront or self.storefront
        params = {"filter[isrc]": isrc}
        data = self._get(f"/catalog/{sf}/songs", params=params)
        return data.get("data", [])

    # ── library songs (favorites) ─────────────────────────────────────────────

    def get_library_songs(self):
        results = []
        path = "/me/library/songs"
        while path:
            data = self._get(path)
            results.extend(data.get("data", []))
            path = data.get("next")
        return results

    def add_to_library(self, catalog_ids):
        params = {"ids[songs]": ",".join(catalog_ids)}
        self._post("/me/library", params=params)


def _resource_to_song(resource):
    """Convert an Apple Music API song resource to an ultrasonics song dict."""
    attr = resource.get("attributes", {})
    catalog_id = resource.get("id", "")
    song = {
        "title":   attr.get("name", ""),
        "artists": [attr.get("artistName")] if attr.get("artistName") else [],
        "album":   attr.get("albumName", ""),
    }
    if attr.get("releaseDate"):
        song["date"] = attr["releaseDate"][:4]
    if attr.get("isrc"):
        song["isrc"] = attr["isrc"]
    art = attr.get("artwork", {})
    if art.get("url"):
        song["image"] = art["url"].replace("{w}", "300").replace("{h}", "300")
    if catalog_id:
        song.setdefault("id", {})["apple music"] = catalog_id
    return song


def _resolve_catalog_id(song, api, storefront, fuzzy_ratio):
    """
    Resolve a song to its Apple Music catalog ID.
    Priority: existing id → ISRC lookup → title/artist search + fuzzy match.
    Flips vault orphan to linked when a match is found.
    """
    # 1. Already have it
    if song.get("id", {}).get("apple music"):
        return song["id"]["apple music"]

    # 2. ISRC
    if song.get("isrc"):
        results = api.get_catalog_by_isrc(song["isrc"], storefront)
        if results:
            cid = results[0]["id"]
            _try_flip_orphan(song, "apple music", cid)
            return cid

    # 3. Search + fuzzy
    artists = song.get("artists") or []
    query = f"{song.get('title', '')} {artists[0] if artists else ''}".strip()
    if not query:
        return None
    for resource in api.search_catalog(query, storefront):
        cand = _resource_to_song(resource)
        if fuzzymatch.similarity(song, cand) >= fuzzy_ratio:
            cid = cand.get("id", {}).get("apple music")
            if cid:
                _try_flip_orphan(song, "apple music", cid)
            return cid

    return None


def _try_flip_orphan(song, platform, platform_id):
    vault_id = (song.get("id") or {}).get("vault")
    if vault_id:
        try:
            from ultrasonics.tools import vault as _vault
            _vault.flip_orphan_to_linked(vault_id, platform, platform_id)
        except Exception:
            pass


def run(settings_dict, **kwargs):
    database   = kwargs["database"]
    component  = kwargs["component"]
    songs_dict = kwargs.get("songs_dict", [])

    media_user_token = database.get("media_user_token", "").strip()
    storefront       = (database.get("storefront") or "us").strip().lower() or "us"
    sync_mode        = (database.get("sync_mode") or "playlists").lower()
    fuzzy_ratio      = _parse_ratio(database.get("fuzzy_ratio"))

    if not media_user_token:
        raise Exception(
            "Apple Music: media-user-token is required. "
            "Copy it from DevTools → Application → Cookies on music.apple.com."
        )

    api = AppleMusicAPI(media_user_token, storefront)

    # ── FAVORITES ────────────────────────────────────────────────────────────

    if sync_mode == "favorites":
        if component == "inputs":
            songs = [_resource_to_song(r) for r in api.get_library_songs()]
            return [{"name": "Apple Music Library", "id": {"apple music": "__library__"}, "songs": songs}]

        for playlist in songs_dict:
            catalog_ids = [
                cid for song in playlist.get("songs", [])
                if (cid := _resolve_catalog_id(song, api, storefront, fuzzy_ratio))
            ]
            for i in range(0, len(catalog_ids), 200):
                api.add_to_library(catalog_ids[i:i + 200])
                time.sleep(0.5)
        return

    # ── PLAYLISTS ────────────────────────────────────────────────────────────

    if component == "inputs":
        output = []
        for pl in api.get_library_playlists():
            attr  = pl.get("attributes", {})
            pl_id = pl.get("id", "")
            songs = [_resource_to_song(r) for r in api.get_playlist_tracks(pl_id)]
            output.append({
                "name": attr.get("name", "Untitled"),
                "id":   {"apple music": pl_id},
                "songs": songs,
            })
        return output

    # outputs
    existing_playlists = {
        p.get("attributes", {}).get("name", "").lower(): p.get("id")
        for p in api.get_library_playlists()
    }

    for playlist in songs_dict:
        pl_name = name_filter.filter(playlist.get("name", "Untitled"))
        pl_id   = existing_playlists.get(pl_name.lower()) or api.create_playlist(pl_name)

        if not pl_id:
            log.error(f"Apple Music: could not create playlist '{pl_name}'")
            continue

        existing_isrcs = {
            r.get("attributes", {}).get("isrc")
            for r in api.get_playlist_tracks(pl_id)
            if r.get("attributes", {}).get("isrc")
        }

        new_ids = [
            cid for song in playlist.get("songs", [])
            if not (song.get("isrc") and song["isrc"] in existing_isrcs)
            if (cid := _resolve_catalog_id(song, api, storefront, fuzzy_ratio))
        ]

        for i in range(0, len(new_ids), 200):
            api.add_tracks_to_playlist(pl_id, new_ids[i:i + 200])
            time.sleep(0.5)

        if new_ids:
            log.info(f"Apple Music: added {len(new_ids)} tracks to '{pl_name}'")


def builder(**kwargs):
    return []


def test(database, **kwargs):
    token      = database.get("media_user_token", "").strip()
    storefront = (database.get("storefront") or "us").strip() or "us"
    if not token:
        log.error("Apple Music: media_user_token is required for test.")
        return False
    try:
        api = AppleMusicAPI(token, storefront)
        playlists = api.get_library_playlists()
        log.info(f"Apple Music test: found {len(playlists)} playlists in {storefront}.")
        return True
    except Exception as exc:
        log.error(f"Apple Music test failed: {exc}")
        return False
