#!/usr/bin/env python3

"""
up_youtube

Input and output plugin for YouTube (playlists via YouTube Data API v3).
Auth: OAuth2. No ISRC — videoId-based matching only.
Searches cost 100 quota units (daily limit 10,000). A 1.8s delay is inserted.
Vault contract: flips orphan→linked on successful match.

Install: pip install google-api-python-client google-auth-oauthlib
"""

import json
import time

from tqdm import tqdm

from ultrasonics import logs
from ultrasonics.tools import matchings, name_filter

log = logs.create_log(__name__)

_AVAILABLE = True
try:
    from googleapiclient.discovery import build as _yt_build
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
    "version": "0.2",
    "settings": [
        {"type": "string",
         "value": "YouTube uses the Data API v3 (official). Paste your OAuth2 token JSON below. No ISRC — matched by title+artist only."},
        {"type": "textarea", "label": "OAuth2 Token JSON", "name": "token_json", "value": ""},
        {"type": "text", "label": "Client ID",     "name": "client_id",     "value": ""},
        {"type": "text", "label": "Client Secret", "name": "client_secret", "value": ""},
        {"type": "text", "label": "Fuzzy Ratio",   "name": "fuzzy_ratio",   "value": "85"},
    ],
}

_SCOPES = ["https://www.googleapis.com/auth/youtube"]
_ID_KEY = "youtube"


def _parse_ratio(val, default=85.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _build_service(database):
    if not _AVAILABLE:
        raise Exception("google-api-python-client not installed. "
                        "pip install google-api-python-client google-auth-oauthlib")
    token_json_str = database.get("token_json", "").strip()
    if not token_json_str:
        raise Exception("YouTube OAuth2 token JSON is required.")
    creds = _Creds.from_authorized_user_info(json.loads(token_json_str), _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(_Request())
    return _yt_build("youtube", "v3", credentials=creds)


def _yt_item_to_songs_dict(snippet):
    vid_id   = snippet.get("resourceId", {}).get("videoId")
    title    = snippet.get("title", "")
    channel  = snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle") or ""
    d = {"title": title}
    if channel:
        d["artists"] = [channel]
    if vid_id:
        d["id"] = {_ID_KEY: vid_id}
    return {k: v for k, v in d.items() if v}


def _list_playlist_items(yt, playlist_id):
    items, page_token = [], None
    while True:
        req = dict(part="snippet", playlistId=playlist_id, maxResults=50)
        if page_token:
            req["pageToken"] = page_token
        resp = yt.playlistItems().list(**req).execute()
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def run(settings_dict, **kwargs):
    if not _AVAILABLE:
        raise Exception("google-api-python-client not installed.")

    database   = kwargs["database"]
    component  = kwargs["component"]
    applet_id  = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    yt          = _build_service(database)
    fuzzy_ratio = _parse_ratio(settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio"))

    from ultrasonics.tools import fuzzymatch

    def _search_video(song):
        """Search YouTube for song. Throttles (1.8s) to protect quota."""
        q = " ".join(filter(None, [song.get("title"), (song.get("artists") or [""])[0]]))
        if not q:
            return None

        # Matchings store first (free, no quota)
        for plat, pid in (song.get("id") or {}).items():
            if plat in ("vault", _ID_KEY) or not pid:
                continue
            learned = matchings.lookup(plat, str(pid), _ID_KEY)
            if learned:
                return learned
        if song.get("isrc"):
            learned = matchings.lookup_by_isrc(song["isrc"], _ID_KEY)
            if learned:
                return learned

        time.sleep(1.8)  # 100 quota units/search; 10k/day → ~5500 searches/day max
        try:
            resp = yt.search().list(
                part="snippet", q=q, type="video",
                videoCategoryId="10", maxResults=5,
            ).execute()
        except Exception as e:
            log.warning(f"YouTube search failed: {e}")
            return None

        best_score, best_id = 0, None
        for item in resp.get("items", []):
            s   = item.get("snippet", {})
            cnd = {"title": s.get("title", ""), "artists": [s.get("channelTitle", "")]}
            score = fuzzymatch.similarity(song, cnd)
            if score and score > best_score:
                best_score, best_id = score, item.get("id", {}).get("videoId")

        if best_score >= fuzzy_ratio and best_id:
            for plat, pid in (song.get("id") or {}).items():
                if plat not in ("vault", _ID_KEY) and pid:
                    matchings.save(plat, str(pid), _ID_KEY, best_id,
                                   src_title=song.get("title"),
                                   src_artist="; ".join(song.get("artists") or []))
            vault_cid = (song.get("id") or {}).get("vault")
            if vault_cid:
                try:
                    from ultrasonics.tools import vault as _vault
                    tl = _vault.get_link(vault_cid, _ID_KEY)
                    if tl and tl["status"] == "orphan":
                        _vault.flip_orphan_to_linked(vault_cid, _ID_KEY, best_id)
                except Exception as ve:
                    log.debug(f"vault flip skipped: {ve}")
            return best_id
        return None

    if component == "inputs":
        playlists_raw, page_token = [], None
        while True:
            req = dict(part="snippet,contentDetails", mine=True, maxResults=50)
            if page_token:
                req["pageToken"] = page_token
            resp = yt.playlists().list(**req).execute()
            playlists_raw.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        result = [{"name": pl["snippet"]["title"], "id": {_ID_KEY: pl["id"]}}
                  for pl in playlists_raw]
        if settings_dict.get("filter"):
            result = name_filter.filter(result, settings_dict["filter"])
        for i, pl in tqdm(enumerate(result), desc="Fetching YouTube playlists"):
            items = _list_playlist_items(yt, pl["id"][_ID_KEY])
            result[i]["songs"] = [_yt_item_to_songs_dict(it.get("snippet", {})) for it in items]
        return result

    else:
        existing_pls, page_token = {}, None
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
            name  = playlist.get("name", "Untitled")
            pl_id = existing_pls.get(name)
            if not pl_id:
                resp  = yt.playlists().insert(
                    part="snippet,status",
                    body={"snippet": {"title": name},
                          "status": {"privacyStatus": "private"}},
                ).execute()
                pl_id = resp["id"]

            existing_items = _list_playlist_items(yt, pl_id)
            existing_ids   = {
                it.get("snippet", {}).get("resourceId", {}).get("videoId")
                for it in existing_items
            }

            for song in tqdm(playlist.get("songs", []), desc=f"Matching '{name}'"):
                try:
                    vid_id = (song.get("id") or {}).get(_ID_KEY)
                    if not vid_id:
                        vid_id = _search_video(song)
                    if vid_id and vid_id not in existing_ids:
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
                    log.warning(f"YouTube add failed for '{song.get('title')}': {e}")


def test(database, **kwargs):
    if not _AVAILABLE:
        raise Exception("google-api-python-client not installed.")
    yt   = _build_service(database)
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
        {"type": "string",
         "value": "Write playlists to YouTube. Each search costs 100 API quota units (daily limit 10,000)."},
        {"type": "radio", "label": "Existing Playlists", "name": "existing_playlists",
         "id": "existing_playlists", "options": ["Append", "Update"], "required": True},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": ""},
    ]
