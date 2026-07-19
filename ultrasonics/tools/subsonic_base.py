#!/usr/bin/env python3

"""
subsonic_base
Shared base class for Subsonic-compatible server adapters
(Navidrome, generic Subsonic).

Provides:
- Token-based auth (_auth_params, _request, _request_multi)
- Full CRUD for playlists, favorites (starred), albums, artists
- songs_dict conversion helpers (track_to_songs_dict, album_to_dict, artist_to_dict)
- vault_resolve_and_match: shared matching loop used by all Subsonic output adapters
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
    Mixin for Subsonic REST API auth and CRUD.

    Subclasses set:
        self.server_url  (str, no trailing slash)
        self.username    (str)
        self.password    (str)
        ID_KEY           (class attr, e.g. "navidrome" or "subsonic")
    """

    _API_VERSION = API_VERSION
    _CLIENT_NAME = CLIENT_NAME
    ID_KEY = "subsonic"

    # ── auth ──────────────────────────────────────────────────────────────────

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
        rp = self._auth_params()
        if params:
            rp.update(params)
        resp = requests.get(url, params=rp, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        inner = data.get("subsonic-response", data)
        if inner.get("status") != "ok":
            err = inner.get("error", {})
            raise Exception(f"Subsonic error {err.get('code')}: {err.get('message')}")
        return inner

    def _request_multi(self, endpoint, list_key, values, extra=None):
        """Request with a repeated query-string key (e.g. songIdToAdd=1&songIdToAdd=2)."""
        url = f"{self.server_url.rstrip('/')}/rest/{endpoint}"
        pairs = list(self._auth_params().items())
        if extra:
            pairs += list(extra.items())
        for v in values:
            pairs.append((list_key, v))
        resp = requests.get(url, params=pairs, timeout=30)
        resp.raise_for_status()
        inner = resp.json().get("subsonic-response", resp.json())
        if inner.get("status") != "ok":
            err = inner.get("error", {})
            raise Exception(f"Subsonic error {err.get('code')}: {err.get('message')}")
        return inner

    def ping(self):
        self._request("ping")

    # ── playlists ─────────────────────────────────────────────────────────────

    def get_playlists(self):
        resp = self._request("getPlaylists")
        pls = resp.get("playlists", {}).get("playlist", [])
        return pls if isinstance(pls, list) else ([pls] if pls else [])

    def get_playlist(self, playlist_id):
        resp = self._request("getPlaylist", {"id": playlist_id})
        return resp.get("playlist", {})

    def create_playlist(self, name):
        resp = self._request("createPlaylist", {"name": name})
        return resp.get("playlist", {}).get("id")

    def update_playlist_add(self, playlist_id, song_ids):
        if not song_ids:
            return
        self._request_multi("updatePlaylist", "songIdToAdd", song_ids,
                            extra={"playlistId": playlist_id})

    def update_playlist_remove(self, playlist_id, indices):
        if not indices:
            return
        self._request_multi("updatePlaylist", "songIndexToRemove", indices,
                            extra={"playlistId": playlist_id})

    # ── favorites (starred) ───────────────────────────────────────────────────

    def get_starred(self):
        resp = self._request("getStarred2")
        songs = resp.get("starred2", {}).get("song", [])
        return songs if isinstance(songs, list) else ([songs] if songs else [])

    def star(self, track_id):
        self._request("star", {"id": track_id})

    def unstar(self, track_id):
        self._request("unstar", {"id": track_id})

    # ── albums ────────────────────────────────────────────────────────────────

    def get_album_list(self, list_type="alphabeticalByName", size=500, offset=0):
        resp = self._request("getAlbumList2", {"type": list_type, "size": size, "offset": offset})
        albums = resp.get("albumList2", {}).get("album", [])
        return albums if isinstance(albums, list) else ([albums] if albums else [])

    def search_albums(self, query, count=10):
        resp = self._request("search3", {"query": query, "albumCount": count,
                                         "songCount": 0, "artistCount": 0})
        results = resp.get("searchResult3", {}).get("album", [])
        return results if isinstance(results, list) else ([results] if results else [])

    # ── artists ───────────────────────────────────────────────────────────────

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
        resp = self._request("search3", {"query": query, "artistCount": count,
                                         "songCount": 0, "albumCount": 0})
        results = resp.get("searchResult3", {}).get("artist", [])
        return results if isinstance(results, list) else ([results] if results else [])

    # ── track search ──────────────────────────────────────────────────────────

    def search_tracks(self, query, count=20):
        resp = self._request("search3", {"query": query, "songCount": count,
                                         "albumCount": 0, "artistCount": 0})
        results = resp.get("searchResult3", {}).get("song", [])
        return results if isinstance(results, list) else ([results] if results else [])

    # ── conversion helpers ────────────────────────────────────────────────────

    def track_to_songs_dict(self, track):
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
            item["id"] = {self.ID_KEY: str(track["id"])}
        return {k: v for k, v in item.items() if v}

    def album_to_dict(self, album):
        item = {"name": album.get("name", ""),
                "id": {self.ID_KEY: str(album["id"])} if album.get("id") else {}}
        if album.get("artist"):
            item["artists"] = [album["artist"]]
        if album.get("year"):
            item["date"] = str(album["year"])
        if album.get("musicBrainzId"):
            item["mbid"] = album["musicBrainzId"]
        return {k: v for k, v in item.items() if v}

    def artist_to_dict(self, artist):
        return {k: v for k, v in {
            "name": artist.get("name", ""),
            "id": {self.ID_KEY: str(artist["id"])} if artist.get("id") else {},
        }.items() if v}

    # ── vault-aware match helper ──────────────────────────────────────────────

    def resolve_track(self, song, fuzzy_ratio, matchings_mod):
        """
        Find this adapter's platform_id for `song`, using:
          1. song["id"][ID_KEY]  (already known)
          2. vault canonical_id lookup via matchings
          3. ISRC lookup via matchings
          4. Fuzzy search on this server

        If the track was previously an orphan (song["id"].get("vault") present and
        no ID_KEY link), flips it to linked in the vault on success.

        Returns platform_id (str) or None.
        """
        # Already have the ID
        try:
            return song["id"][self.ID_KEY]
        except KeyError:
            pass

        # Matchings store lookup (cross-platform)
        for plat, pid in (song.get("id") or {}).items():
            if plat in ("vault", self.ID_KEY) or not pid:
                continue
            learned = matchings_mod.lookup(plat, str(pid), self.ID_KEY)
            if learned:
                return learned

        if song.get("isrc"):
            learned = matchings_mod.lookup_by_isrc(song["isrc"], self.ID_KEY)
            if learned:
                return learned

        # Fuzzy search
        title = (song.get("title") or "").strip()
        if not title:
            return None
        artist = (song.get("artists") or [""])[0]
        results = self.search_tracks(f"{title} {artist}".strip(), count=10)

        from ultrasonics.tools import fuzzymatch as _fm
        best_score, best_id = 0, None
        for r in results:
            score = _fm.similarity(song, self.track_to_songs_dict(r))
            if score and score > best_score:
                best_score, best_id = score, r.get("id")

        if best_score >= fuzzy_ratio and best_id:
            best_id = str(best_id)
            # Save to matchings store
            for plat, pid in (song.get("id") or {}).items():
                if plat not in ("vault", self.ID_KEY) and pid:
                    matchings_mod.save(
                        plat, str(pid), self.ID_KEY, best_id,
                        src_isrc=song.get("isrc"),
                        src_title=song.get("title"),
                        src_artist="; ".join(song.get("artists") or []),
                    )
            # If vault track was orphaned for this platform, flip it
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
