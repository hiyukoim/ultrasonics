#!/usr/bin/env python3

"""
up_subsonic

Input and output plugin for generic Subsonic-compatible servers
(Airsonic, Gonic, etc.). Shares implementation with up_navidrome
via SubsonicBase; dispatches on sync_mode.
"""

import json
import os
import sqlite3
import time

from tqdm import tqdm

from app import _ultrasonics
from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter
from ultrasonics.tools.subsonic_base import SubsonicBase

log = logs.create_log(__name__)

handshake = {
    "name": "subsonic",
    "description": "sync playlists, favorites, albums, and artists to/from any subsonic-compatible server",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.1",
    "settings": [
        {
            "type": "text",
            "label": "Server URL",
            "name": "server_url",
            "value": "e.g. http://localhost:4040",
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


class _API(SubsonicBase):
    ID_KEY = "subsonic"

    def __init__(self, server_url, username, password):
        self.server_url = server_url
        self.username = username
        self.password = password


def run(settings_dict, **kwargs):
    """Dispatch on sync_mode; identical logic to up_navidrome but uses id key 'subsonic'."""
    database = kwargs["database"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")

    if not server_url or not username or not password:
        raise Exception("Subsonic plugin requires server URL, username, and password.")

    api = _API(server_url, username, password)
    sync_mode = database.get("sync_mode", "playlists")

    fuzzy_ratio = 85
    try:
        fuzzy_ratio = float(
            settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio") or 85
        )
    except (ValueError, TypeError):
        pass

    ID_KEY = "subsonic"

    def _search_and_match(song):
        try:
            return song["id"][ID_KEY]
        except KeyError:
            pass
        ids = song.get("id", {})
        for plat, pid in ids.items():
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
        results = api.search(q, count=10)
        best_score, best_id = 0, None
        for r in results:
            score = fuzzymatch.similarity(song, api.track_to_songs_dict(r, ID_KEY))
            if score > best_score:
                best_score, best_id = score, r.get("id")
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
            starred = api.get_starred()
            tracks = [api.track_to_songs_dict(t, ID_KEY) for t in starred]
            return [{"name": "Favorites", "id": {ID_KEY: "__favorites__"}, "songs": tracks}]
        else:
            existing = {t.get("id") for t in api.get_starred()}
            for playlist in songs_dict:
                for song in playlist.get("songs", []):
                    mid = _search_and_match(song)
                    if mid and mid not in existing:
                        try:
                            api.star(mid)
                            existing.add(mid)
                        except Exception as e:
                            log.warning(f"star failed: {e}")

    elif sync_mode == "albums":
        if component == "inputs":
            all_albums, offset = [], 0
            while True:
                batch = api.get_album_list(size=500, offset=offset)
                if not batch:
                    break
                all_albums.extend(batch)
                offset += len(batch)
                if len(batch) < 500:
                    break
            return [{"name": "Albums", "id": {ID_KEY: "__albums__"},
                     "songs": [api.album_to_dict(a, ID_KEY) for a in all_albums]}]
        else:
            for playlist in songs_dict:
                for album_item in playlist.get("songs", []):
                    q = " ".join(filter(None, [
                        (album_item.get("artists") or [""])[0],
                        album_item.get("name", ""),
                    ]))
                    results = api.search_albums(q, count=10)
                    for r in results:
                        r_dict = api.album_to_dict(r, ID_KEY)
                        if album_item.get("mbid") and r.get("musicBrainzId") and album_item["mbid"] == r["musicBrainzId"]:
                            log.info(f"Album '{album_item.get('name')}' matched by MBID.")
                            break
                        if fuzzymatch.similarity(album_item, r_dict) >= fuzzy_ratio:
                            log.info(f"Album '{album_item.get('name')}' fuzzy matched.")
                            break

    elif sync_mode == "artists":
        if component == "inputs":
            artists = api.get_artists()
            return [{"name": "Artists", "id": {ID_KEY: "__artists__"},
                     "songs": [api.artist_to_dict(a, ID_KEY) for a in artists]}]
        else:
            for playlist in songs_dict:
                for artist_item in playlist.get("songs", []):
                    name = artist_item.get("name", "")
                    if not name:
                        continue
                    results = api.search_artists(name, count=5)
                    for r in results:
                        if fuzzymatch.similarity(
                            {"title": name, "artists": [name]},
                            {"title": r.get("name", ""), "artists": [r.get("name", "")]},
                        ) >= fuzzy_ratio:
                            log.info(f"Artist '{name}' matched.")
                            break

    else:
        # playlists
        if component == "inputs":
            playlists = api.get_playlists()
            result = []
            for pl in playlists:
                item = {"name": pl.get("name", "Untitled"), "id": {ID_KEY: str(pl.get("id", ""))}}
                result.append(item)
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching Subsonic playlists"):
                data = api.get_playlist(pl["id"][ID_KEY])
                entries = data.get("entry", [])
                if isinstance(entries, dict):
                    entries = [entries]
                result[i]["songs"] = [api.track_to_songs_dict(e, ID_KEY) for e in entries]
            return result
        else:
            existing_pls = api.get_playlists()
            existing_names = {pl.get("name", ""): pl.get("id") for pl in existing_pls}

            for playlist in songs_dict:
                name = playlist.get("name", "Untitled")
                pl_id = existing_names.get(name) or api.create_playlist(name)
                if not pl_id:
                    log.error(f"Could not create playlist '{name}'")
                    continue

                existing_data = api.get_playlist(pl_id)
                existing_entries = existing_data.get("entry", [])
                if isinstance(existing_entries, dict):
                    existing_entries = [existing_entries]
                existing_tracks = [api.track_to_songs_dict(e, ID_KEY) for e in existing_entries]
                existing_ids = [e.get("id") for e in existing_entries]

                to_add = []
                for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                    if fuzzymatch.duplicate(song, existing_tracks, fuzzy_ratio):
                        continue
                    mid = _search_and_match(song)
                    if mid and mid not in existing_ids:
                        to_add.append(mid)
                api.update_playlist_add(pl_id, to_add)


def test(database, **kwargs):
    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")
    if not all([server_url, username, password]):
        raise Exception("Server URL, username, and password are required.")
    api = _API(server_url, username, password)
    api.ping()
    log.info(f"Connected to Subsonic server at {server_url}")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch from this Subsonic-compatible server."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": "Write to this Subsonic-compatible server."},
        {
            "type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
            "id": "existing_playlists", "options": ["Append", "Update"], "required": True,
        },
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
