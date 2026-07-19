#!/usr/bin/env python3

"""
subsonic_base
Shared base class for Subsonic-compatible server adapters
(Navidrome, generic Subsonic, Jellyfin-Subsonic proxy).

Provides token-auth, request wrapping, and songs-dict conversion.
"""

import hashlib
import secrets

import requests

from ultrasonics import logs

log = logs.create_log(__name__)

API_VERSION = "1.16.1"
CLIENT_NAME = "ultrasonics"


class SubsonicBase:
    """
    Mixin that handles Subsonic REST API auth and common CRUD operations.

    Subclasses must set `self.server_url`, `self.username`, `self.password`
    before calling any method.
    """

    _API_VERSION = API_VERSION
    _CLIENT_NAME = CLIENT_NAME

    def _auth_params(self):
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": self._API_VERSION,
            "c": self._CLIENT_NAME,
            "f": "json",
        }

    def _request(self, endpoint, params=None):
        url = f"{self.server_url.rstrip('/')}/rest/{endpoint}"
        request_params = self._auth_params()
        if params:
            request_params.update(params)

        resp = requests.get(url, params=request_params, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        inner = data.get("subsonic-response", data)

        if inner.get("status") != "ok":
            error = inner.get("error", {})
            raise Exception(
                f"Subsonic API error {error.get('code')}: {error.get('message')}"
            )

        return inner

    def _request_multi(self, endpoint, params_list_key, values, extra_params=None):
        """Issue a single request where one param is multi-valued (e.g. songIdToAdd)."""
        url = f"{self.server_url.rstrip('/')}/rest/{endpoint}"
        auth = self._auth_params()
        pairs = list(auth.items())
        if extra_params:
            pairs += list(extra_params.items())
        for v in values:
            pairs.append((params_list_key, v))

        resp = requests.get(url, params=pairs, timeout=30)
        resp.raise_for_status()
        inner = resp.json().get("subsonic-response", resp.json())
        if inner.get("status") != "ok":
            error = inner.get("error", {})
            raise Exception(
                f"Subsonic API error {error.get('code')}: {error.get('message')}"
            )
        return inner

    def ping(self):
        self._request("ping")

    # ── playlists ──────────────────────────────────────────────────────────

    def get_playlists(self):
        resp = self._request("getPlaylists")
        pls = resp.get("playlists", {}).get("playlist", [])
        return pls if isinstance(pls, list) else [pls]

    def get_playlist(self, playlist_id):
        resp = self._request("getPlaylist", {"id": playlist_id})
        return resp.get("playlist", {})

    def create_playlist(self, name):
        resp = self._request("createPlaylist", {"name": name})
        return resp.get("playlist", {}).get("id")

    def update_playlist_add(self, playlist_id, song_ids):
        if not song_ids:
            return
        self._request_multi(
            "updatePlaylist",
            "songIdToAdd",
            song_ids,
            extra_params={"playlistId": playlist_id},
        )

    def update_playlist_remove(self, playlist_id, indices):
        if not indices:
            return
        self._request_multi(
            "updatePlaylist",
            "songIndexToRemove",
            indices,
            extra_params={"playlistId": playlist_id},
        )

    # ── favorites ──────────────────────────────────────────────────────────

    def get_starred(self):
        resp = self._request("getStarred2")
        songs = resp.get("starred2", {}).get("song", [])
        return songs if isinstance(songs, list) else [songs]

    def star(self, track_id):
        self._request("star", {"id": track_id})

    def unstar(self, track_id):
        self._request("unstar", {"id": track_id})

    # ── albums ─────────────────────────────────────────────────────────────

    def get_album_list(self, list_type="alphabeticalByName", size=500, offset=0):
        resp = self._request("getAlbumList2", {
            "type": list_type, "size": size, "offset": offset,
        })
        albums = resp.get("albumList2", {}).get("album", [])
        return albums if isinstance(albums, list) else [albums]

    def get_album(self, album_id):
        resp = self._request("getAlbum", {"id": album_id})
        return resp.get("album", {})

    def search_albums(self, query, count=10):
        resp = self._request("search3", {
            "query": query, "albumCount": count, "songCount": 0, "artistCount": 0,
        })
        results = resp.get("searchResult3", {}).get("album", [])
        return results if isinstance(results, list) else [results]

    # ── artists ────────────────────────────────────────────────────────────

    def get_artists(self):
        resp = self._request("getArtists")
        index_list = resp.get("artists", {}).get("index", [])
        if isinstance(index_list, dict):
            index_list = [index_list]
        artists = []
        for idx in index_list:
            entries = idx.get("artist", [])
            if isinstance(entries, dict):
                entries = [entries]
            artists.extend(entries)
        return artists

    def search_artists(self, query, count=10):
        resp = self._request("search3", {
            "query": query, "artistCount": count, "songCount": 0, "albumCount": 0,
        })
        results = resp.get("searchResult3", {}).get("artist", [])
        return results if isinstance(results, list) else [results]

    # ── search / conversion ────────────────────────────────────────────────

    def search(self, query, count=20):
        resp = self._request("search3", {
            "query": query, "songCount": count, "albumCount": 0, "artistCount": 0,
        })
        results = resp.get("searchResult3", {}).get("song", [])
        return results if isinstance(results, list) else [results]

    def track_to_songs_dict(self, track, id_key="subsonic"):
        """Convert a Subsonic track object to ultrasonics songs_dict format."""
        item = {"title": track.get("title", "")}
        if track.get("artist"):
            item["artists"] = [track["artist"]]
        if track.get("album"):
            item["album"] = track["album"]
        if track.get("year"):
            item["date"] = str(track["year"])
        if track.get("path"):
            item["location"] = track["path"]
        if track.get("id"):
            item["id"] = {id_key: str(track["id"])}
        return {k: v for k, v in item.items() if v}

    def album_to_dict(self, album, id_key="subsonic"):
        item = {
            "name": album.get("name", ""),
            "id": {id_key: str(album["id"])} if album.get("id") else {},
        }
        if album.get("artist"):
            item["artists"] = [album["artist"]]
        if album.get("year"):
            item["date"] = str(album["year"])
        if album.get("musicBrainzId"):
            item["mbid"] = album["musicBrainzId"]
        return {k: v for k, v in item.items() if v}

    def artist_to_dict(self, artist, id_key="subsonic"):
        item = {
            "name": artist.get("name", ""),
            "id": {id_key: str(artist["id"])} if artist.get("id") else {},
        }
        return {k: v for k, v in item.items() if v}
