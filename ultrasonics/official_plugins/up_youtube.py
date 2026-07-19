#!/usr/bin/env python3

"""
up_youtube

Input and output plugin for YouTube (playlists via YouTube Data API v3).
Auth: OAuth2 Authorization Code. No ISRC — videoId-based matching only.
caps: PpTt (playlist/track read+write), no album/artist.
max: 5000. tpt: ~1.78s (API quota = 10,000 units/day; search costs 100).

Install: pip install google-api-python-client google-auth-oauthlib
"""

import json
import os
import time

from tqdm import tqdm

from app import _ultrasonics
from ultrasonics import logs
from ultrasonics.tools import fuzzymatch, matchings, name_filter

log = logs.create_log(__name__)

_AVAILABLE = True
try:
    from googleapiclient.discovery import build as _yt_build
    from googleapiclient.errors import HttpError as _YtHttpError
    from google.oauth2.credentials import Credentials as _Creds
    from google.auth.transport.requests import Request as _Request
except ImportError:
    _AVAILABLE = False
    log.warning("google-api-python-client not installed — up_youtube unavailable.")

handshake = {
    "name": "youtube",
    "description": "sync playlists to/from youtube (data api v3 — oauth2)",
    "type": ["inputs", "outputs"],
    "mode": ["playlists"],
    "version": "0.1",
    "settings": [
        {
            "type": "string",
            "value": "YouTube uses the Data API v3 (official). Paste your OAuth2 token JSON below. No ISRC — matched by title+artist only.",
        },
        {
            "type": "textarea",
            "label": "OAuth2 Token JSON",
            "name": "token_json",
            "value": "",
        },
        {
            "type": "text",
            "label": "Client ID",
            "name": "client_id",
            "value": "",
        },
        {
            "type": "text",
            "label": "Client Secret",
            "name": "client_secret",
            "value": "",
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": "Recommended: 85",
        },
    ],
}

_SCOPES = ["https://www.googleapis.com/auth/youtube"]


def _build_service(database):
    if not _AVAILABLE:
        raise Exception("google-api-python-client not installed. pip install google-api-python-client google-auth-oauthlib")
    token_json_str = database.get("token_json", "").strip()
    if not token_json_str:
        raise Exception("YouTube OAuth2 token JSON is required.")
    token_data = json.loads(token_json_str)
    creds = _Creds.from_authorized_user_info(token_data, _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(_Request())
    return _yt_build("youtube", "v3", credentials=creds)


def _yt_item_to_songs_dict(snippet):
    title = snippet.get("title", "")
    channel = snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle") or ""
    resource = snippet.get("resourceId", {})
    video_id = resource.get("videoId")
    d = {"title": title}
    if channel:
        d["artists"] = [channel]
    if video_id:
        d["id"] = {"youtube": video_id}
    return {k: v for k, v in d.items() if v}


def run(settings_dict, **kwargs):
    if not _AVAILABLE:
        raise Exception("google-api-python-client not installed.")

    database = kwargs["database"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    yt = _build_service(database)

    fuzzy_ratio = 85
    try:
        fuzzy_ratio = float(
            settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio") or 85
        )
    except (ValueError, TypeError):
        pass

    ID_KEY = "youtube"

    def _get_my_channel_id():
        resp = yt.channels().list(part="id", mine=True).execute()
        items = resp.get("items", [])
        return items[0]["id"] if items else None

    def _search_video(song):
        q = " ".join(filter(None, [song.get("title"), (song.get("artists") or [""])[0]]))
        if not q:
            return None
        # Each search costs 100 quota units — throttle here
        time.sleep(1.8)
        try:
            resp = yt.search().list(
                part="snippet", q=q, type="video", videoCategoryId="10",
                maxResults=5,
            ).execute()
        except Exception as e:
            log.warning(f"YouTube search failed: {e}")
            return None
        items = resp.get("items", [])
        best_score, best_id = 0, None
        for item in items:
            s = item.get("snippet", {})
            candidate = {
                "title": s.get("title", ""),
                "artists": [s.get("channelTitle", "")],
            }
            score = fuzzymatch.similarity(song, candidate)
            if score > best_score:
                best_score, best_id = score, item.get("id", {}).get("videoId")
        if best_score >= fuzzy_ratio and best_id:
            for plat, pid in (song.get("id") or {}).items():
                if plat != ID_KEY:
                    matchings.save(plat, pid, ID_KEY, best_id,
                                   src_title=song.get("title"),
                                   src_artist="; ".join(song.get("artists", [])))
            return best_id
        return None

    def _list_playlist_items(playlist_id):
        items = []
        page_token = None
        while True:
            kwargs_req = dict(part="snippet", playlistId=playlist_id, maxResults=50)
            if page_token:
                kwargs_req["pageToken"] = page_token
            resp = yt.playlistItems().list(**kwargs_req).execute()
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items

    if component == "inputs":
        channel_id = _get_my_channel_id()
        playlists_raw = []
        page_token = None
        while True:
            req = dict(part="snippet,contentDetails", mine=True, maxResults=50)
            if page_token:
                req["pageToken"] = page_token
            resp = yt.playlists().list(**req).execute()
            playlists_raw.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        result = []
        for pl in playlists_raw:
            result.append({
                "name": pl["snippet"]["title"],
                "id": {ID_KEY: pl["id"]},
            })
        if settings_dict.get("filter"):
            result = name_filter.filter(result, settings_dict["filter"])

        for i, pl in tqdm(enumerate(result), desc="Fetching YouTube playlists"):
            items = _list_playlist_items(pl["id"][ID_KEY])
            result[i]["songs"] = [_yt_item_to_songs_dict(it.get("snippet", {})) for it in items]
        return result

    else:
        existing_pls = {}
        page_token = None
        while True:
            req = dict(part="snippet", mine=True, maxResults=50)
            if page_token:
                req["pageToken"] = page_token
            resp = yt.playlists().list(**req).execute()
            for pl in resp.get("items", []):
                existing_pls[pl["snippet"]["title"]] = pl["id"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        for playlist in songs_dict:
            name = playlist.get("name", "Untitled")
            pl_id = existing_pls.get(name)
            if not pl_id:
                resp = yt.playlists().insert(
                    part="snippet,status",
                    body={"snippet": {"title": name}, "status": {"privacyStatus": "private"}},
                ).execute()
                pl_id = resp["id"]

            existing_items = _list_playlist_items(pl_id)
            existing_ids = {
                it.get("snippet", {}).get("resourceId", {}).get("videoId")
                for it in existing_items
            }

            for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                vid_id = (song.get("id") or {}).get(ID_KEY)
                if not vid_id:
                    for plat, pid in (song.get("id") or {}).items():
                        if plat != ID_KEY:
                            vid_id = matchings.lookup(plat, pid, ID_KEY)
                            if vid_id:
                                break
                if not vid_id:
                    vid_id = _search_video(song)
                if vid_id and vid_id not in existing_ids:
                    try:
                        yt.playlistItems().insert(
                            part="snippet",
                            body={"snippet": {
                                "playlistId": pl_id,
                                "resourceId": {"kind": "youtube#video", "videoId": vid_id},
                            }},
                        ).execute()
                        existing_ids.add(vid_id)
                        time.sleep(0.5)
                    except Exception as e:
                        log.warning(f"YouTube add item failed: {e}")


def test(database, **kwargs):
    if not _AVAILABLE:
        raise Exception("google-api-python-client not installed.")
    yt = _build_service(database)
    resp = yt.channels().list(part="id", mine=True).execute()
    if not resp.get("items"):
        raise Exception("YouTube auth OK but no channel found.")
    log.info("YouTube connection OK.")


def builder(**kwargs):
    component = kwargs["component"]
    if component == "inputs":
        return [
            {"type": "string", "value": "Fetch playlists from YouTube."},
            {"type": "text", "label": "Filter", "name": "filter", "value": ""},
        ]
    return [
        {"type": "string", "value": "Write playlists to YouTube. Searches cost API quota (100 units/search, daily limit 10,000)."},
        {
            "type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
            "id": "existing_playlists", "options": ["Append", "Update"], "required": True,
        },
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
