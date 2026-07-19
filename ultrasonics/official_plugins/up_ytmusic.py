#!/usr/bin/env python3

"""
up_ytmusic ⚠️

Input and output plugin for YouTube Music.
Uses ytmusicapi (unofficial Python library — requires cookie-based auth).
No ISRC: matching is title+artist fuzzy only.
Vault contract: flips orphan→linked on successful match via resolver.

Install: pip install ytmusicapi
Auth: run `ytmusicapi oauth` or `ytmusicapi browser` to create headers_auth.json,
then paste the JSON content into the "Auth JSON" setting.

WARNING: This plugin uses an unofficial API. It may break without notice.
"""

import json
import os
import tempfile

from tqdm import tqdm

from ultrasonics import logs
from ultrasonics.tools import matchings, name_filter
from ultrasonics.tools.resolver import resolve_track

log = logs.create_log(__name__)

_AVAILABLE = True
try:
    import ytmusicapi
except ImportError:
    _AVAILABLE = False
    log.warning("ytmusicapi not installed — up_ytmusic unavailable. pip install ytmusicapi")

handshake = {
    "name": "ytmusic",
    "description": "⚠️ sync playlists and favorites to/from youtube music (unofficial api)",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites"],
    "version": "0.2",
    "settings": [
        {"type": "string",
         "value": "⚠️ YouTube Music uses an unofficial API via ytmusicapi. Paste the contents of your headers_auth.json file below."},
        {"type": "textarea", "label": "Auth JSON (headers_auth.json content)",
         "name": "auth_json", "value": ""},
        {"type": "select", "label": "Sync Mode", "name": "sync_mode",
         "options": ["playlists", "favorites"], "value": "playlists"},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": "85"},
    ],
}

_ID_KEY = "ytmusic"


def _parse_ratio(val, default=85.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _get_api(auth_json_str):
    if not _AVAILABLE:
        raise Exception("ytmusicapi is not installed. Run: pip install ytmusicapi")
    auth_data = json.loads(auth_json_str)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(auth_data, tmp)
    tmp.close()
    try:
        ytm = ytmusicapi.YTMusic(tmp.name)
    finally:
        os.unlink(tmp.name)
    return ytm


def _to_songs_dict(track):
    title   = track.get("title", "")
    artists = [a["name"] for a in track.get("artists", []) if a.get("name")]
    album   = (track.get("album") or {}).get("name") if isinstance(track.get("album"), dict) else track.get("album")
    vid_id  = track.get("videoId")
    d = {"title": title}
    if artists:
        d["artists"] = artists
    if album:
        d["album"] = album
    if vid_id:
        d["id"] = {_ID_KEY: vid_id}
    return {k: v for k, v in d.items() if v}


def run(settings_dict, **kwargs):
    if not _AVAILABLE:
        raise Exception("ytmusicapi not installed — cannot run up_ytmusic.")

    database   = kwargs["database"]
    component  = kwargs["component"]
    applet_id  = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    auth_json_str = database.get("auth_json", "").strip()
    if not auth_json_str:
        raise Exception("YouTube Music Auth JSON is not configured.")

    try:
        ytm = _get_api(auth_json_str)
    except Exception as e:
        raise Exception(f"YouTube Music auth failed: {e}")

    sync_mode   = database.get("sync_mode", "playlists")
    fuzzy_ratio = _parse_ratio(settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio"))

    def _search(q):
        return ytm.search(q, filter="songs", limit=10)

    def _match(song):
        return resolve_track(song, _ID_KEY, _search, _to_songs_dict, fuzzy_ratio, matchings)

    if sync_mode == "favorites":
        if component == "inputs":
            try:
                liked  = ytm.get_liked_songs(limit=5000)
                tracks = [_to_songs_dict(t) for t in liked.get("tracks", [])]
            except Exception as e:
                raise Exception(f"Failed to fetch YTMusic liked songs: {e}")
            return [{"name": "Favorites", "id": {_ID_KEY: "__favorites__"}, "songs": tracks}]

        for playlist in songs_dict:
            for song in playlist.get("songs", []):
                try:
                    mid = _match(song)
                    if mid:
                        ytm.rate_song(mid, "LIKE")
                except Exception as e:
                    log.warning(f"YTMusic like failed for '{song.get('title')}': {e}")

    else:
        # playlists
        if component == "inputs":
            try:
                playlists_raw = ytm.get_library_playlists(limit=25)
            except Exception as e:
                raise Exception(f"Failed to fetch YTMusic playlists: {e}")
            result = [{"name": pl.get("title", "Untitled"),
                       "id": {_ID_KEY: pl.get("playlistId")}}
                      for pl in playlists_raw]
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching YTMusic playlists"):
                try:
                    full   = ytm.get_playlist(pl["id"][_ID_KEY], limit=5000)
                    result[i]["songs"] = [_to_songs_dict(t) for t in full.get("tracks", [])]
                except Exception as e:
                    log.warning(f"YTMusic fetch playlist '{pl['name']}' failed: {e}")
                    result[i]["songs"] = []
            return result

        try:
            existing_pls = {pl.get("title", ""): pl.get("playlistId")
                            for pl in ytm.get_library_playlists(limit=25)}
        except Exception as e:
            raise Exception(f"Failed to list YTMusic playlists: {e}")

        for playlist in songs_dict:
            name  = playlist.get("name", "Untitled")
            pl_id = existing_pls.get(name)
            if not pl_id:
                try:
                    pl_id = ytm.create_playlist(name, name)
                except Exception as e:
                    log.error(f"Failed to create YTMusic playlist '{name}': {e}")
                    continue

            to_add = []
            for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                try:
                    mid = _match(song)
                    if mid:
                        to_add.append(mid)
                except Exception as e:
                    log.warning(f"YTMusic match failed for '{song.get('title')}': {e}")
            if to_add:
                try:
                    ytm.add_playlist_items(pl_id, to_add)
                except Exception as e:
                    log.warning(f"YTMusic add items failed: {e}")


def test(database, **kwargs):
    if not _AVAILABLE:
        raise Exception("ytmusicapi not installed.")
    auth_json_str = database.get("auth_json", "").strip()
    if not auth_json_str:
        raise Exception("Auth JSON is required.")
    ytm = _get_api(auth_json_str)
    ytm.get_library_playlists(limit=1)
    log.info("YouTube Music connection OK.")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "⚠️ Fetch playlists or liked songs from YouTube Music (unofficial API)."},
            {"type": "text", "label": "Filter (playlists mode)", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": "⚠️ Write to YouTube Music (unofficial API). No ISRC — fuzzy match only."},
        {"type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
         "id": "existing_playlists", "options": ["Append", "Update"], "required": True},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
