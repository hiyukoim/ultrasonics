#!/usr/bin/env python3

"""
up_navidrome

Input and output plugin for Navidrome (and any Subsonic-compatible server).
Reads playlists and songs from a Navidrome/Subsonic server, and can create
or update playlists on it.

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
from ultrasonics.tools import fuzzymatch, name_filter

log = logs.create_log(__name__)

handshake = {
    "name": "navidrome",
    "description": "sync playlists to and from a navidrome or subsonic server",
    "type": ["inputs", "outputs"],
    "mode": ["playlists"],
    "version": "0.1",
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

    Inputs mode: fetches playlists from the server, returns songs_dict.
    Outputs mode: creates or updates playlists on the server.
    """

    database = kwargs["database"]
    global_settings = kwargs["global_settings"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    class Subsonic:
        """
        Handles interactions with the Subsonic/Navidrome REST API.
        """

        API_VERSION = "1.16.1"
        CLIENT_NAME = "ultrasonics"

        def __init__(self, server_url, username, password):
            self.server_url = server_url.rstrip("/")
            self.username = username
            self.password = password

        def _auth_params(self):
            """Generate token-based auth parameters (Subsonic API 1.13.0+)."""
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
            """Make a GET request to the Subsonic API."""
            url = f"{self.server_url}/rest/{endpoint}"
            request_params = self._auth_params()
            if params:
                request_params.update(params)

            resp = requests.get(url, params=request_params, timeout=30)
            resp.raise_for_status()

            data = resp.json()

            # Subsonic wraps responses in "subsonic-response"
            inner = data.get("subsonic-response", data)

            if inner.get("status") != "ok":
                error = inner.get("error", {})
                raise Exception(
                    f"Subsonic API error {error.get('code')}: {error.get('message')}"
                )

            return inner

        def get_playlists(self):
            """Get all playlists from the server."""
            resp = self._request("getPlaylists")
            playlists = resp.get("playlists", {}).get("playlist", [])
            if isinstance(playlists, dict):
                playlists = [playlists]
            return playlists

        def get_playlist(self, playlist_id):
            """Get a specific playlist with its tracks."""
            resp = self._request("getPlaylist", {"id": playlist_id})
            return resp.get("playlist", {})

        def create_playlist(self, name):
            """Create a new playlist, return its ID."""
            resp = self._request("createPlaylist", {"name": name})
            playlist = resp.get("playlist", {})
            return playlist.get("id")

        def update_playlist(self, playlist_id, song_ids_to_add=None, song_indices_to_remove=None):
            """
            Update a playlist. Subsonic uses index-based removal.
            song_ids_to_add: list of song IDs to append.
            song_indices_to_remove: list of integer indices to remove.
            """
            params = {"playlistId": playlist_id}

            if song_ids_to_add:
                params["songIdToAdd"] = song_ids_to_add
            if song_indices_to_remove:
                params["songIndexToRemove"] = song_indices_to_remove

            # Subsonic updatePlaylist uses multiple same-name params;
            # requests handles this via list of tuples
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

            data = resp.json()
            inner = data.get("subsonic-response", data)
            if inner.get("status") != "ok":
                error = inner.get("error", {})
                raise Exception(
                    f"Subsonic API error {error.get('code')}: {error.get('message')}"
                )

        def search(self, query, count=20):
            """Search for songs on the server."""
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
            """
            Convert a Subsonic/Navidrome track entry to ultrasonics songs_dict format.
            """
            title = track.get("title", "")
            artists = []
            if track.get("artist"):
                artists = [track["artist"]]
            album = track.get("album")
            date = track.get("year")
            if date:
                date = str(date)
            duration_ms = track.get("duration")
            if duration_ms:
                duration_ms = duration_ms * 1000

            item = {
                "title": title,
                "artists": artists,
            }

            if album:
                item["album"] = album
            if date:
                item["date"] = date

            # Navidrome may expose path as a location reference
            if track.get("path"):
                item["location"] = track["path"]

            # Store the subsonic ID for direct matching later
            track_id = track.get("id")
            if track_id:
                item["id"] = {"navidrome": str(track_id)}

            # Remove empty fields
            item = {k: v for k, v in item.items() if v}

            return item

    class UnmatchedStore:
        """
        SQLite store for tracks that could not be matched on the destination.
        Re-attempted on each run; removed once matched successfully.
        """

        def __init__(self):
            db_dir = os.path.join(_ultrasonics["config_dir"], "up_navidrome")
            try:
                os.makedirs(db_dir, exist_ok=True)
            except OSError:
                pass
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
            """Return list of (rowid, song_dict) for pending unmatched tracks."""
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT rowid, song_json FROM unmatched "
                    "WHERE applet_id = ? AND playlist_id = ? AND status = 'pending'",
                    (applet_id, playlist_id),
                )
                rows = cursor.fetchall()
            return [(row[0], json.loads(row[1])) for row in rows]

        def mark_resolved(self, rowid):
            """Remove a successfully matched track."""
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM unmatched WHERE rowid = ?", (rowid,))
                conn.commit()

        def upsert(self, applet_id, playlist_id, song):
            """Insert or update an unmatched track."""
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

    # Instantiate the API client
    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")

    if not server_url or not username or not password:
        raise Exception(
            "Navidrome plugin is not configured. Please set server URL, username, and password in plugin settings."
        )

    api = Subsonic(server_url, username, password)

    unmatched_store = UnmatchedStore()

    if component == "inputs":
        # Get all playlists from the server
        playlists = api.get_playlists()

        songs_dict = []
        for pl in playlists:
            item = {
                "name": pl.get("name", "Untitled"),
                "id": {"navidrome": str(pl.get("id", ""))},
            }
            songs_dict.append(item)

        # Apply name filter if provided
        if settings_dict.get("filter"):
            songs_dict = name_filter.filter(songs_dict, settings_dict["filter"])

        # Fetch tracks for each playlist
        log.info("Fetching tracks from Navidrome playlists...")
        for i, playlist in tqdm(
            enumerate(songs_dict), desc="Fetching Navidrome playlists"
        ):
            playlist_data = api.get_playlist(playlist["id"]["navidrome"])
            entries = playlist_data.get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]

            tracks = [api.subsonic_to_songs_dict(entry) for entry in entries]
            songs_dict[i]["songs"] = tracks

        return songs_dict

    else:
        # Outputs mode
        fuzzy_ratio = 85
        try:
            fuzzy_ratio = float(settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio") or 85)
        except (ValueError, TypeError):
            pass

        # Get existing playlists for matching
        existing_playlists = api.get_playlists()
        existing_names = {pl.get("name", ""): pl.get("id") for pl in existing_playlists}

        for playlist in songs_dict:
            playlist_name = playlist.get("name", "Untitled")

            # Check if playlist already exists
            if playlist_name in existing_names:
                playlist_id = existing_names[playlist_name]
                log.info(f"Playlist '{playlist_name}' exists, updating.")
            else:
                # Try matching by navidrome ID
                playlist_id = None
                try:
                    nav_id = playlist["id"]["navidrome"]
                    if nav_id in [str(pl.get("id")) for pl in existing_playlists]:
                        playlist_id = nav_id
                except KeyError:
                    pass

                if not playlist_id:
                    log.info(f"Creating new playlist: {playlist_name}")
                    playlist_id = api.create_playlist(playlist_name)

            if not playlist_id:
                log.error(f"Failed to create or find playlist '{playlist_name}', skipping.")
                continue

            # Get existing tracks in the playlist
            existing_data = api.get_playlist(playlist_id)
            existing_entries = existing_data.get("entry", [])
            if isinstance(existing_entries, dict):
                existing_entries = [existing_entries]

            existing_tracks = [api.subsonic_to_songs_dict(e) for e in existing_entries]
            existing_ids = [e.get("id", "") for e in existing_entries]

            def _search_and_match(song):
                """
                Try to find a matching track on the server.
                Returns the navidrome track ID on success, None on failure.
                """
                # Try direct navidrome ID
                try:
                    nav_id = song["id"]["navidrome"]
                    return nav_id
                except KeyError:
                    pass

                # Search the server for a match
                query_parts = []
                if song.get("title"):
                    query_parts.append(song["title"])
                if song.get("artists"):
                    query_parts.append(song["artists"][0])

                if not query_parts:
                    return None

                query = " ".join(query_parts)
                results = api.search(query, count=10)

                if not results:
                    return None

                best_score = 0
                best_id = None

                for result in results:
                    result_song = api.subsonic_to_songs_dict(result)
                    score = fuzzymatch.similarity(song, result_song)
                    if score > best_score:
                        best_score = score
                        best_id = result.get("id")

                if best_score >= fuzzy_ratio and best_id:
                    return best_id
                return None

            # Find songs to add
            song_ids_to_add = []

            # Re-attempt previously unmatched tracks first
            pending = unmatched_store.get_pending(applet_id, playlist_id)
            if pending:
                log.info(f"Re-attempting {len(pending)} previously unmatched tracks...")
                for rowid, song in pending:
                    is_duplicate = fuzzymatch.duplicate(song, existing_tracks, fuzzy_ratio)
                    if is_duplicate:
                        unmatched_store.mark_resolved(rowid)
                        continue
                    matched_id = _search_and_match(song)
                    if matched_id and matched_id not in existing_ids:
                        song_ids_to_add.append(matched_id)
                        unmatched_store.mark_resolved(rowid)
                    else:
                        unmatched_store.upsert(applet_id, playlist_id, song)

            log.info(f"Matching songs for playlist '{playlist_name}'...")
            for song in tqdm(
                playlist.get("songs", []),
                desc=f"Matching songs for '{playlist_name}'",
            ):
                # Check if song already exists in playlist via fuzzy match
                is_duplicate = fuzzymatch.duplicate(song, existing_tracks, fuzzy_ratio)
                if is_duplicate:
                    continue

                matched_id = _search_and_match(song)

                if matched_id:
                    if matched_id not in existing_ids:
                        song_ids_to_add.append(matched_id)
                else:
                    log.debug(
                        f"Could not match '{song.get('title', '?')}', storing as unmatched."
                    )
                    unmatched_store.upsert(applet_id, playlist_id, song)

            # Handle update mode — remove songs not in source
            if settings_dict.get("existing_playlists") == "Update" and existing_entries:
                source_ids = set(song_ids_to_add)
                # Identify indices of existing tracks that are NOT in the new source
                # For update mode, we remove tracks that aren't in the incoming list
                indices_to_remove = []
                for idx, entry in enumerate(existing_entries):
                    entry_song = api.subsonic_to_songs_dict(entry)
                    found_in_source = False
                    for song in playlist.get("songs", []):
                        score = fuzzymatch.similarity(song, entry_song)
                        if score >= fuzzy_ratio:
                            found_in_source = True
                            break
                    if not found_in_source:
                        indices_to_remove.append(idx)

                if indices_to_remove:
                    log.info(f"Removing {len(indices_to_remove)} songs from '{playlist_name}'.")
                    api.update_playlist(playlist_id, song_indices_to_remove=indices_to_remove)

            # Add new songs
            if song_ids_to_add:
                log.info(f"Adding {len(song_ids_to_add)} songs to '{playlist_name}'.")
                # Subsonic supports batch add
                api.update_playlist(playlist_id, song_ids_to_add=song_ids_to_add)
            else:
                log.info(f"No new songs to add to '{playlist_name}'.")


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

    data = resp.json()
    inner = data.get("subsonic-response", data)

    if inner.get("status") != "ok":
        error = inner.get("error", {})
        raise Exception(f"Server responded with error: {error.get('message', 'unknown')}")

    log.info(f"Successfully connected to {server_url}")


def builder(**kwargs):
    component = kwargs["component"]

    if component == "inputs":
        settings_dict = [
            {
                "type": "string",
                "value": "Fetch playlists from your Navidrome or Subsonic-compatible server.",
            },
            {
                "type": "string",
                "value": "Use a regex filter to select specific playlists by name. Leave blank to sync all.",
            },
            {
                "type": "text",
                "label": "Filter",
                "name": "filter",
                "value": "",
            },
        ]
        return settings_dict

    else:
        settings_dict = [
            {
                "type": "string",
                "value": "Write playlists to your Navidrome or Subsonic-compatible server. If a playlist with the same name exists, it will be updated.",
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
                "type": "string",
                "value": "Override the global fuzzy ratio for matching songs on this server. Leave blank to use the global setting.",
            },
            {
                "type": "text",
                "label": "Fuzzy Ratio",
                "name": "fuzzy_ratio",
                "value": "",
            },
        ]
        return settings_dict
