#!/usr/bin/env python3

"""
up_ytmusic ⚠️

Input and output plugin for YouTube Music.
Uses ytmusicapi (unofficial Python library — requires cookie-based auth).
No ISRC: matching is title+artist fuzzy only.

Install: pip install ytmusicapi
Auth: run `ytmusicapi oauth` or `ytmusicapi browser` to create headers_auth.json,
then paste the JSON content into the "Auth JSON" setting.

WARNING: This plugin uses an unofficial API. It may break without notice.
"""

import json
import os
import tempfile

from tqdm import tqdm

from app import _ultrasonics
from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter

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
    "version": "0.1",
    "settings": [
        {
            "type": "string",
            "value": "⚠️ YouTube Music uses an unofficial API via ytmusicapi. Paste the contents of your headers_auth.json file below.",
        },
        {
            "type": "textarea",
            "label": "Auth JSON (headers_auth.json content)",
            "name": "auth_json",
            "value": "",
        },
        {
            "type": "select",
            "label": "Sync Mode",
            "name": "sync_mode",
            "options": ["playlists", "favorites"],
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


def _get_api(auth_json_str):
    if not _AVAILABLE:
        raise Exception("ytmusicapi is not installed. Run: pip install ytmusicapi")
    try:
        auth_data = json.loads(auth_json_str)
    except Exception:
        raise Exception("Invalid Auth JSON for YouTube Music.")
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(auth_data, tmp)
    tmp.close()
    try:
        ytm = ytmusicapi.YTMusic(tmp.name)
    finally:
        os.unlink(tmp.name)
    return ytm


def _yt_to_songs_dict(track):
    title = track.get("title", "")
    artists = [a["name"] for a in track.get("artists", []) if a.get("name")]
    album = track.get("album", {}).get("name") if isinstance(track.get("album"), dict) else track.get("album")
    video_id = track.get("videoId")
    d = {"title": title}
    if artists:
        d["artists"] = artists
    if album:
        d["album"] = album
    if video_id:
        d["id"] = {"ytmusic": video_id}
    return {k: v for k, v in d.items() if v}


def run(settings_dict, **kwargs):
    if not _AVAILABLE:
        raise Exception("ytmusicapi not installed — cannot run up_ytmusic.")

    database = kwargs["database"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    auth_json_str = database.get("auth_json", "").strip()
    if not auth_json_str:
        raise Exception("YouTube Music Auth JSON is not configured.")

    try:
        ytm = _get_api(auth_json_str)
    except Exception as e:
        raise Exception(f"YouTube Music auth failed: {e}")

    sync_mode = database.get("sync_mode", "playlists")
    fuzzy_ratio = 85
    try:
        fuzzy_ratio = float(
            settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio") or 85
        )
    except (ValueError, TypeError):
        pass

    ID_KEY = "ytmusic"

    def _match(song):
        try:
            return song["id"][ID_KEY]
        except KeyError:
            pass
        for plat, pid in (song.get("id") or {}).items():
            if plat != ID_KEY:
                learned = matchings.lookup(plat, pid, ID_KEY)
                if learned:
                    return learned
        q = " ".join(filter(None, [song.get("title"), (song.get("artists") or [""])[0]]))
        if not q:
            return None
        try:
            results = ytm.search(q, filter="songs", limit=10)
        except Exception as e:
            log.warning(f"YTMusic search failed: {e}")
            return None
        best_score, best_id = 0, None
        for r in results:
            score = fuzzymatch.similarity(song, _yt_to_songs_dict(r))
            if score > best_score:
                best_score, best_id = score, r.get("videoId")
        if best_score >= fuzzy_ratio and best_id:
            for plat, pid in (song.get("id") or {}).items():
                if plat != ID_KEY:
                    matchings.save(plat, pid, ID_KEY, best_id,
                                   src_title=song.get("title"),
                                   src_artist="; ".join(song.get("artists", [])))
            return best_id
        return None

    if sync_mode == "favorites":
        if component == "inputs":
            try:
                liked = ytm.get_liked_songs(limit=5000)
                tracks = [_yt_to_songs_dict(t) for t in liked.get("tracks", [])]
            except Exception as e:
                raise Exception(f"Failed to fetch YTMusic liked songs: {e}")
            return [{"name": "Favorites", "id": {ID_KEY: "__favorites__"}, "songs": tracks}]
        else:
            for playlist in songs_dict:
                for song in playlist.get("songs", []):
                    mid = _match(song)
                    if mid:
                        try:
                            ytm.rate_song(mid, "LIKE")
                        except Exception as e:
                            log.warning(f"YTMusic rate failed: {e}")

    else:
        # playlists
        if component == "inputs":
            try:
                playlists_raw = ytm.get_library_playlists(limit=25)
            except Exception as e:
                raise Exception(f"Failed to fetch YTMusic playlists: {e}")
            result = []
            for pl in playlists_raw:
                pl_id = pl.get("playlistId")
                result.append({"name": pl.get("title", "Untitled"), "id": {ID_KEY: pl_id}})
            if settings_dict.get("filter"):
                result = name_filter.filter(result, settings_dict["filter"])
            for i, pl in tqdm(enumerate(result), desc="Fetching YTMusic playlists"):
                try:
                    full = ytm.get_playlist(pl["id"][ID_KEY], limit=5000)
                    tracks = full.get("tracks", [])
                    result[i]["songs"] = [_yt_to_songs_dict(t) for t in tracks]
                except Exception as e:
                    log.warning(f"Failed to fetch YTMusic playlist '{pl['name']}': {e}")
                    result[i]["songs"] = []
            return result
        else:
            try:
                existing_pls = {pl.get("title", ""): pl.get("playlistId")
                                for pl in ytm.get_library_playlists(limit=25)}
            except Exception as e:
                raise Exception(f"Failed to list YTMusic playlists: {e}")

            for playlist in songs_dict:
                name = playlist.get("name", "Untitled")
                pl_id = existing_pls.get(name)
                if not pl_id:
                    try:
                        pl_id = ytm.create_playlist(name, name)
                    except Exception as e:
                        log.error(f"Failed to create YTMusic playlist '{name}': {e}")
                        continue

                to_add = []
                for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                    mid = _match(song)
                    if mid:
                        to_add.append(mid)
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
    # Basic connectivity test
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
        {
            "type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
            "id": "existing_playlists", "options": ["Append", "Update"], "required": True,
        },
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
