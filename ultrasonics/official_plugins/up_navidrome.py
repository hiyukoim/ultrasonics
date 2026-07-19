#!/usr/bin/env python3

"""
up_navidrome

Input and output plugin for Navidrome (and any Subsonic-compatible server).
Supports playlists, favorites (starred tracks), albums, and artists.

Uses the Subsonic REST API with token-based authentication.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time

import requests
from tqdm import tqdm

from app import _ultrasonics
from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter

log = logs.create_log(__name__)

handshake = {
    "name": "navidrome",
    "description": "sync playlists, favorites, albums, and artists to/from a navidrome or subsonic server",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.3",
    "settings": [
        {
            "type": "text",
            "label": "Server URL",
            "name": "server_url",
            "value": "e.g. http://localhost:4533",
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
            "type": "string",
            "value": "Fuzzy matching is used when searching for songs without a direct ID match. A value of 100 means fields must be identical; a value of 0 means anything qualifies as a match.",
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": "Recommended: 85",
        },
    ],
}


def run(settings_dict, **kwargs):
    """
    Runs the up_navidrome plugin.

    Dispatches on sync_mode (playlists / favorites / albums / artists).
    Inputs: fetches from the server, returns songs_dict.
    Outputs: writes back to the server.
    """

    database = kwargs["database"]
    global_settings = kwargs["global_settings"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    class Subsonic:
        """Handles interactions with the Subsonic/Navidrome REST API."""

        API_VERSION = "1.16.1"
        CLIENT_NAME = "ultrasonics"

        def __init__(self, server_url, username, password):
            self.server_url = server_url.rstrip("/")
            self.username = username
            self.password = password

        def _auth_params(self):
            salt = secrets.token_hex(8)
            token = hashlib.md5((self.password + salt).encode()).hexdigest()
            return {
                "u": self.username,
                "t": token,
                "s": salt,
                "v": self.API_VERSION,
                "c": self.CLIENT_NAME,
                "f": "json",
            }

        def _request(self, endpoint, params=None):
            url = f"{self.server_url}/rest/{endpoint}"
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

        # ── playlists ──────────────────────────────────────────────────────

        def get_playlists(self):
            resp = self._request("getPlaylists")
            playlists = resp.get("playlists", {}).get("playlist", [])
            if isinstance(playlists, dict):
                playlists = [playlists]
            return playlists

        def get_playlist(self, playlist_id):
            resp = self._request("getPlaylist", {"id": playlist_id})
            return resp.get("playlist", {})

        def create_playlist(self, name):
            resp = self._request("createPlaylist", {"name": name})
            return resp.get("playlist", {}).get("id")

        def update_playlist(self, playlist_id, song_ids_to_add=None, song_indices_to_remove=None):
            params = {"playlistId": playlist_id}
            if song_ids_to_add:
                params["songIdToAdd"] = song_ids_to_add
            if song_indices_to_remove:
                params["songIndexToRemove"] = song_indices_to_remove

            param_tuples = []
            for k, v in params.items():
                if isinstance(v, list):
                    for item in v:
                        param_tuples.append((k, item))
                else:
                    param_tuples.append((k, v))

            url = f"{self.server_url}/rest/updatePlaylist"
            auth = self._auth_params()
            for k, v in auth.items():
                param_tuples.append((k, v))

            resp = requests.get(url, params=param_tuples, timeout=30)
            resp.raise_for_status()
            inner = resp.json().get("subsonic-response", resp.json())
            if inner.get("status") != "ok":
                error = inner.get("error", {})
                raise Exception(
                    f"Subsonic API error {error.get('code')}: {error.get('message')}"
                )

        # ── favorites (starred) ────────────────────────────────────────────

        def get_starred(self):
            """Return starred songs via getStarred2."""
            resp = self._request("getStarred2")
            starred = resp.get("starred2", {})
            songs = starred.get("song", [])
            if isinstance(songs, dict):
                songs = [songs]
            return songs

        def star(self, track_id):
            self._request("star", {"id": track_id})

        def unstar(self, track_id):
            self._request("unstar", {"id": track_id})

        # ── albums ─────────────────────────────────────────────────────────

        def get_album_list(self, list_type="alphabeticalByName", size=500, offset=0):
            """Return albums via getAlbumList2."""
            resp = self._request("getAlbumList2", {
                "type": list_type,
                "size": size,
                "offset": offset,
            })
            albums = resp.get("albumList2", {}).get("album", [])
            if isinstance(albums, dict):
                albums = [albums]
            return albums

        def get_album(self, album_id):
            resp = self._request("getAlbum", {"id": album_id})
            return resp.get("album", {})

        def search_albums(self, query, count=10):
            resp = self._request("search3", {
                "query": query,
                "albumCount": count,
                "songCount": 0,
                "artistCount": 0,
            })
            results = resp.get("searchResult3", {}).get("album", [])
            if isinstance(results, dict):
                results = [results]
            return results

        # ── artists ────────────────────────────────────────────────────────

        def get_artists(self):
            """Return all artists via getArtists."""
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

        def get_artist(self, artist_id):
            resp = self._request("getArtist", {"id": artist_id})
            return resp.get("artist", {})

        def search_artists(self, query, count=10):
            resp = self._request("search3", {
                "query": query,
                "artistCount": count,
                "songCount": 0,
                "albumCount": 0,
            })
            results = resp.get("searchResult3", {}).get("artist", [])
            if isinstance(results, dict):
                results = [results]
            return results

        # ── search & conversion ────────────────────────────────────────────

        def search(self, query, count=20):
            resp = self._request("search3", {
                "query": query,
                "songCount": count,
                "albumCount": 0,
                "artistCount": 0,
            })
            results = resp.get("searchResult3", {}).get("song", [])
            if isinstance(results, dict):
                results = [results]
            return results

        def subsonic_to_songs_dict(self, track):
            title = track.get("title", "")
            artists = [track["artist"]] if track.get("artist") else []
            album = track.get("album")
            date = str(track["year"]) if track.get("year") else None
            item = {"title": title, "artists": artists}
            if album:
                item["album"] = album
            if date:
                item["date"] = date
            if track.get("path"):
                item["location"] = track["path"]
            if track.get("id"):
                item["id"] = {"navidrome": str(track["id"])}
            return {k: v for k, v in item.items() if v}

        def album_to_dict(self, album):
            """Convert a Subsonic album entry to a minimal songs_dict-style dict."""
            item = {
                "name": album.get("name", ""),
                "artists": [album["artist"]] if album.get("artist") else [],
                "id": {"navidrome": str(album["id"])} if album.get("id") else {},
            }
            if album.get("year"):
                item["date"] = str(album["year"])
            return item

        def artist_to_dict(self, artist):
            item = {
                "name": artist.get("name", ""),
                "id": {"navidrome": str(artist["id"])} if artist.get("id") else {},
            }
            return item

    class UnmatchedStore:
        def __init__(self):
            db_dir = os.path.join(_ultrasonics["config_dir"], "up_navidrome")
            os.makedirs(db_dir, exist_ok=True)
            self.db_path = os.path.join(db_dir, "unmatched.db")
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS unmatched ("
                    "  applet_id TEXT,"
                    "  playlist_id TEXT,"
                    "  song_json TEXT,"
                    "  first_seen INTEGER,"
                    "  last_tried INTEGER,"
                    "  status TEXT DEFAULT 'pending',"
                    "  UNIQUE(applet_id, playlist_id, song_json)"
                    ")"
                )
                conn.commit()

        def get_pending(self, applet_id, playlist_id):
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT rowid, song_json FROM unmatched "
                    "WHERE applet_id = ? AND playlist_id = ? AND status = 'pending'",
                    (applet_id, playlist_id),
                )
                rows = cursor.fetchall()
            return [(row[0], json.loads(row[1])) for row in rows]

        def mark_resolved(self, rowid):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM unmatched WHERE rowid = ?", (rowid,))
                conn.commit()

        def upsert(self, applet_id, playlist_id, song):
            song_json = json.dumps(song, ensure_ascii=False, sort_keys=True)
            now = int(time.time())
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO unmatched (applet_id, playlist_id, song_json, first_seen, last_tried, status) "
                    "VALUES (?, ?, ?, ?, ?, 'pending') "
                    "ON CONFLICT(applet_id, playlist_id, song_json) "
                    "DO UPDATE SET last_tried = ?, status = 'pending'",
                    (applet_id, playlist_id, song_json, now, now, now),
                )
                conn.commit()

    # ── init ────────────────────────────────────────────────────────────────

    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")

    if not server_url or not username or not password:
        raise Exception(
            "Navidrome plugin is not configured. Please set server URL, username, and password."
        )

    api = Subsonic(server_url, username, password)
    unmatched_store = UnmatchedStore()

    sync_mode = database.get("sync_mode", "playlists")

    fuzzy_ratio = 85
    try:
        fuzzy_ratio = float(
            settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio") or 85
        )
    except (ValueError, TypeError):
        pass

    # ── helpers shared across modes ─────────────────────────────────────────

    def _detect_src_platform(song):
        ids = song.get("id", {})
        for platform in ("spotify", "deezer", "lastfm", "plex", "csv"):
            if platform in ids:
                return platform
        return next(iter(ids), None) if ids else None

    def _get_src_id(song, platform):
        return song.get("id", {}).get(platform) if platform else None

    def _search_and_match_track(song):
        """Return navidrome track ID for song, or None."""
        try:
            return song["id"]["navidrome"]
        except KeyError:
            pass

        src_platform = _detect_src_platform(song)
        src_id = _get_src_id(song, src_platform)
        if src_platform and src_id:
            learned = matchings.lookup(src_platform, src_id, "navidrome")
            if learned:
                return learned
        if song.get("isrc"):
            learned = matchings.lookup_by_isrc(song["isrc"], "navidrome")
            if learned:
                return learned

        query_parts = []
        if song.get("title"):
            query_parts.append(song["title"])
        if song.get("artists"):
            query_parts.append(song["artists"][0])
        if not query_parts:
            return None

        results = api.search(" ".join(query_parts), count=10)
        best_score, best_id = 0, None
        for result in results:
            score = fuzzymatch.similarity(song, api.subsonic_to_songs_dict(result))
            if score > best_score:
                best_score, best_id = score, result.get("id")

        if best_score >= fuzzy_ratio and best_id:
            if src_platform and src_id:
                matchings.save(
                    src_platform, src_id, "navidrome", best_id,
                    src_isrc=song.get("isrc"),
                    src_title=song.get("title"),
                    src_artist="; ".join(song.get("artists", [])),
                )
            return best_id
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # FAVORITES
    # ═══════════════════════════════════════════════════════════════════════

    if sync_mode == "favorites":
        if component == "inputs":
            starred = api.get_starred()
            tracks = [api.subsonic_to_songs_dict(t) for t in starred]
            return [{"name": "Favorites", "id": {"navidrome": "__favorites__"}, "songs": tracks}]

        else:
            # Outputs: star incoming tracks, unstar removed ones if Update mode
            existing_starred = api.get_starred()
            existing_ids = {t.get("id") for t in existing_starred}

            for playlist in songs_dict:
                for song in playlist.get("songs", []):
                    matched_id = _search_and_match_track(song)
                    if matched_id and matched_id not in existing_ids:
                        try:
                            api.star(matched_id)
                            existing_ids.add(matched_id)
                        except Exception as e:
                            log.warning(f"Failed to star {song.get('title')}: {e}")
                    elif not matched_id:
                        log.debug(f"Could not match '{song.get('title')}' for starring.")

                if settings_dict.get("existing_playlists") == "Update":
                    # Unstar anything not in the incoming set
                    incoming_ids = set()
                    for song in playlist.get("songs", []):
                        mid = _search_and_match_track(song)
                        if mid:
                            incoming_ids.add(mid)
                    for track_id in existing_ids - incoming_ids:
                        try:
                            api.unstar(track_id)
                        except Exception as e:
                            log.warning(f"Failed to unstar {track_id}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # ALBUMS
    # ═══════════════════════════════════════════════════════════════════════

    elif sync_mode == "albums":
        if component == "inputs":
            # Page through all albums
            all_albums = []
            offset = 0
            while True:
                batch = api.get_album_list(size=500, offset=offset)
                if not batch:
                    break
                all_albums.extend(batch)
                offset += len(batch)
                if len(batch) < 500:
                    break

            album_dicts = []
            for album in all_albums:
                d = api.album_to_dict(album)
                # Fetch UPC if Navidrome exposes it (musicBrainzId proxy)
                if album.get("musicBrainzId"):
                    d["mbid"] = album["musicBrainzId"]
                album_dicts.append(d)

            return [{"name": "Albums", "id": {"navidrome": "__albums__"}, "songs": album_dicts}]

        else:
            # Outputs: match incoming albums on UPC→musicBrainzId, else name+artist
            for playlist in songs_dict:
                for album_item in playlist.get("songs", []):
                    # Try matching by name+artist via search
                    query = album_item.get("name", "")
                    if album_item.get("artists"):
                        query = f"{album_item['artists'][0]} {query}"

                    results = api.search_albums(query, count=10)
                    best_score, best_match = 0, None
                    for r in results:
                        r_dict = api.album_to_dict(r)
                        # UPC match via musicBrainzId when both sides carry it
                        if album_item.get("mbid") and r.get("musicBrainzId"):
                            if album_item["mbid"] == r["musicBrainzId"]:
                                best_match = r
                                best_score = 101
                                break
                        # Fuzzy name+artist
                        score = fuzzymatch.similarity(album_item, r_dict)
                        if score > best_score:
                            best_score, best_match = score, r

                    if best_score >= fuzzy_ratio and best_match:
                        log.info(
                            f"Album '{album_item.get('name')}' matched on server (score={best_score:.0f})."
                        )
                    else:
                        log.debug(
                            f"Album '{album_item.get('name')}' not found on server."
                        )

    # ═══════════════════════════════════════════════════════════════════════
    # ARTISTS
    # ═══════════════════════════════════════════════════════════════════════

    elif sync_mode == "artists":
        if component == "inputs":
            all_artists = api.get_artists()
            artist_dicts = [api.artist_to_dict(a) for a in all_artists]
            return [{"name": "Artists", "id": {"navidrome": "__artists__"}, "songs": artist_dicts}]

        else:
            # Outputs: match incoming artists by name
            for playlist in songs_dict:
                for artist_item in playlist.get("songs", []):
                    name = artist_item.get("name", "")
                    if not name:
                        continue
                    results = api.search_artists(name, count=5)
                    best_score, best_match = 0, None
                    for r in results:
                        r_name = r.get("name", "")
                        score = fuzzymatch.similarity(
                            {"title": name, "artists": [name]},
                            {"title": r_name, "artists": [r_name]},
                        )
                        if score > best_score:
                            best_score, best_match = score, r

                    if best_score >= fuzzy_ratio and best_match:
                        log.info(
                            f"Artist '{name}' matched (score={best_score:.0f})."
                        )
                    else:
                        log.debug(f"Artist '{name}' not found on server.")

    # ═══════════════════════════════════════════════════════════════════════
    # PLAYLISTS (default)
    # ═══════════════════════════════════════════════════════════════════════

    else:
        if component == "inputs":
            playlists = api.get_playlists()
            songs_dict = []
            for pl in playlists:
                item = {
                    "name": pl.get("name", "Untitled"),
                    "id": {"navidrome": str(pl.get("id", ""))},
                }
                songs_dict.append(item)

            if settings_dict.get("filter"):
                songs_dict = name_filter.filter(songs_dict, settings_dict["filter"])

            log.info("Fetching tracks from Navidrome playlists...")
            for i, playlist in tqdm(enumerate(songs_dict), desc="Fetching Navidrome playlists"):
                playlist_data = api.get_playlist(playlist["id"]["navidrome"])
                entries = playlist_data.get("entry", [])
                if isinstance(entries, dict):
                    entries = [entries]
                songs_dict[i]["songs"] = [api.subsonic_to_songs_dict(e) for e in entries]

            return songs_dict

        else:
            existing_playlists = api.get_playlists()
            existing_names = {pl.get("name", ""): pl.get("id") for pl in existing_playlists}

            for playlist in songs_dict:
                playlist_name = playlist.get("name", "Untitled")

                if playlist_name in existing_names:
                    playlist_id = existing_names[playlist_name]
                else:
                    playlist_id = None
                    try:
                        nav_id = playlist["id"]["navidrome"]
                        if nav_id in [str(pl.get("id")) for pl in existing_playlists]:
                            playlist_id = nav_id
                    except KeyError:
                        pass
                    if not playlist_id:
                        playlist_id = api.create_playlist(playlist_name)

                if not playlist_id:
                    log.error(f"Failed to create or find playlist '{playlist_name}', skipping.")
                    continue

                existing_data = api.get_playlist(playlist_id)
                existing_entries = existing_data.get("entry", [])
                if isinstance(existing_entries, dict):
                    existing_entries = [existing_entries]
                existing_tracks = [api.subsonic_to_songs_dict(e) for e in existing_entries]
                existing_ids = [e.get("id", "") for e in existing_entries]

                song_ids_to_add = []

                pending = unmatched_store.get_pending(applet_id, playlist_id)
                if pending:
                    for rowid, song in pending:
                        if fuzzymatch.duplicate(song, existing_tracks, fuzzy_ratio):
                            unmatched_store.mark_resolved(rowid)
                            continue
                        matched_id = _search_and_match_track(song)
                        if matched_id and matched_id not in existing_ids:
                            song_ids_to_add.append(matched_id)
                            unmatched_store.mark_resolved(rowid)
                        else:
                            unmatched_store.upsert(applet_id, playlist_id, song)

                for song in tqdm(playlist.get("songs", []), desc=f"Matching '{playlist_name}'"):
                    if fuzzymatch.duplicate(song, existing_tracks, fuzzy_ratio):
                        continue
                    matched_id = _search_and_match_track(song)
                    if matched_id:
                        if matched_id not in existing_ids:
                            song_ids_to_add.append(matched_id)
                    else:
                        unmatched_store.upsert(applet_id, playlist_id, song)

                if settings_dict.get("existing_playlists") == "Update" and existing_entries:
                    indices_to_remove = []
                    for idx, entry in enumerate(existing_entries):
                        entry_song = api.subsonic_to_songs_dict(entry)
                        found = any(
                            fuzzymatch.similarity(song, entry_song) >= fuzzy_ratio
                            for song in playlist.get("songs", [])
                        )
                        if not found:
                            indices_to_remove.append(idx)
                    if indices_to_remove:
                        api.update_playlist(playlist_id, song_indices_to_remove=indices_to_remove)

                if song_ids_to_add:
                    api.update_playlist(playlist_id, song_ids_to_add=song_ids_to_add)


def test(database, **kwargs):
    """Test the connection to the Navidrome/Subsonic server."""
    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")

    if not server_url or not username or not password:
        raise Exception("Server URL, username, and password are required.")

    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()

    params = {
        "u": username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "ultrasonics",
        "f": "json",
    }

    resp = requests.get(f"{server_url.rstrip('/')}/rest/ping", params=params, timeout=10)
    resp.raise_for_status()

    inner = resp.json().get("subsonic-response", resp.json())
    if inner.get("status") != "ok":
        error = inner.get("error", {})
        raise Exception(f"Server responded with error: {error.get('message', 'unknown')}")

    log.info(f"Successfully connected to {server_url}")


def search(query, database, **kwargs):
    """
    Search for tracks on the Navidrome server matching `query`.
    Used by the manual matching UI.
    """
    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")

    if not server_url or not username or not password:
        return []

    try:
        salt = secrets.token_hex(8)
        token = hashlib.md5((password + salt).encode()).hexdigest()

        class _API:
            API_VERSION = "1.16.1"
            CLIENT_NAME = "ultrasonics"

        api_tmp = type("Subsonic", (), {
            "server_url": server_url,
            "username": username,
            "password": password,
        })()

        from ultrasonics.official_plugins import up_navidrome as _self
        # Re-use the inner Subsonic class by instantiating via the module-level run closure
        # Simpler: just make a direct request here
        url = f"{server_url.rstrip('/')}/rest/search3"
        params = {
            "u": username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "ultrasonics",
            "f": "json",
            "query": query,
            "songCount": 20,
            "albumCount": 0,
            "artistCount": 0,
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        inner = resp.json().get("subsonic-response", resp.json())
        results = inner.get("searchResult3", {}).get("song", [])
        if isinstance(results, dict):
            results = [results]

        def _to_songs_dict(track):
            item = {
                "title": track.get("title", ""),
                "artists": [track["artist"]] if track.get("artist") else [],
            }
            if track.get("album"):
                item["album"] = track["album"]
            if track.get("id"):
                item["id"] = {"navidrome": str(track["id"])}
            return {k: v for k, v in item.items() if v}

        return [_to_songs_dict(r) for r in results]
    except Exception as e:
        log.warning(f"Navidrome search failed: {e}")
        return []


def builder(**kwargs):
    component = kwargs["component"]

    if component == "inputs":
        return [
            {
                "type": "string",
                "value": "Fetch playlists, favorites, albums, or artists from your Navidrome server. Set Sync Mode in plugin settings.",
            },
            {
                "type": "text",
                "label": "Filter (playlists mode only)",
                "name": "filter",
                "value": "",
            },
        ]

    else:
        return [
            {
                "type": "string",
                "value": "Write to your Navidrome server. For playlists, existing ones will be updated or appended based on the setting below.",
            },
            {
                "type": "radio",
                "label": "Existing Playlists",
                "name": "existing_playlists",
                "id": "existing_playlists",
                "options": ["Append", "Update"],
                "required": True,
            },
            {
                "type": "text",
                "label": "Fuzzy Ratio",
                "name": "fuzzy_ratio",
                "value": "",
            },
        ]
