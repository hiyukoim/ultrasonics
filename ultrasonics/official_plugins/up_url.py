#!/usr/bin/env python3

"""
up_url
Input plugin that imports a playlist from a public URL.
Supports Spotify and Deezer public playlist URLs.
Unknown platforms return a clear error.
"""

import re

import requests

from ultrasonics import logs

log = logs.create_log(__name__)

handshake = {
    "name": "url",
    "description": "import a playlist from a public URL (Spotify, Deezer)",
    "type": ["inputs"],
    "mode": ["playlists"],
    "version": "0.1",
    "settings": [
        {
            "type": "string",
            "value": "Paste a public playlist URL. Supported: Spotify, Deezer. "
                     "For Spotify editorial playlists, use an exportify CSV export instead.",
        },
        {
            "type": "text",
            "label": "Playlist URL",
            "name": "playlist_url",
            "value": "",
        },
        {
            "type": "text",
            "label": "Playlist Name (optional override)",
            "name": "playlist_name",
            "value": "",
        },
    ],
}


def _detect_platform(url):
    """Return (platform, native_id) or raise ValueError."""
    # Spotify: https://open.spotify.com/playlist/ID
    m = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", url)
    if m:
        return "spotify", m.group(1)

    # Deezer: https://www.deezer.com/*/playlist/ID
    m = re.search(r"deezer\.com/(?:[a-z]+/)?playlist/(\d+)", url)
    if m:
        return "deezer", m.group(1)

    raise ValueError(f"Unsupported or unrecognised playlist URL: {url!r}. "
                     "Supported platforms: Spotify, Deezer.")


def _fetch_spotify(playlist_id):
    """Fetch a public Spotify playlist using the unauthenticated embed endpoint."""
    # Use the public embed API — no auth required for public playlists
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}"
    # Attempt with client credentials from environment
    import os
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

    token = None
    if client_id and client_secret:
        token_resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            timeout=10,
        )
        if token_resp.status_code == 200:
            token = token_resp.json().get("access_token")

    if not token:
        raise ValueError(
            "Spotify client credentials (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET) "
            "are required to fetch public playlists. Set them in your environment or "
            "use an exportify CSV export instead."
        )

    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code == 404:
        raise ValueError(f"Spotify playlist not found: {playlist_id!r}. "
                         "Editorial/official playlists may require exportify CSV import.")
    resp.raise_for_status()
    data = resp.json()

    name = data.get("name", f"Spotify/{playlist_id}")
    songs = []
    items = data.get("tracks", {}).get("items", [])
    for item in items:
        track = item.get("track")
        if not track:
            continue
        songs.append({
            "title": track.get("name", ""),
            "artists": [a["name"] for a in track.get("artists", [])],
            "album": (track.get("album") or {}).get("name", ""),
            "date": (track.get("album") or {}).get("release_date", "")[:4],
            "isrc": (track.get("external_ids") or {}).get("isrc", ""),
            "id": {"spotify": track.get("id", "")},
        })

    return name, playlist_id, songs


def _fetch_deezer(playlist_id):
    """Fetch a public Deezer playlist via the public API (no auth required)."""
    resp = requests.get(
        f"https://api.deezer.com/playlist/{playlist_id}",
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise ValueError(f"Deezer error: {data['error'].get('message', str(data['error']))}")

    name = data.get("title", f"Deezer/{playlist_id}")
    songs = []
    for track in data.get("tracks", {}).get("data", []):
        songs.append({
            "title": track.get("title", ""),
            "artists": [track.get("artist", {}).get("name", "")],
            "album": (track.get("album") or {}).get("title", ""),
            "date": "",
            "isrc": track.get("isrc", ""),
            "id": {"deezer": str(track.get("id", ""))},
        })

    return name, str(playlist_id), songs


def run(settings_dict, **kwargs):
    component = kwargs.get("component")
    if component != "inputs":
        return []

    url = (settings_dict.get("playlist_url") or "").strip()
    name_override = (settings_dict.get("playlist_name") or "").strip()

    if not url:
        log.warning("up_url: no URL provided")
        return []

    try:
        platform, native_id = _detect_platform(url)
    except ValueError as e:
        log.error(str(e))
        return []

    try:
        if platform == "spotify":
            name, pid, songs = _fetch_spotify(native_id)
        elif platform == "deezer":
            name, pid, songs = _fetch_deezer(native_id)
        else:
            log.error(f"up_url: unsupported platform {platform!r}")
            return []
    except Exception as e:
        log.error(f"up_url fetch failed: {e}")
        return []

    playlist_name = name_override or name
    log.info(f"up_url: imported {len(songs)} tracks from {platform} — '{playlist_name}'")

    return [{"name": playlist_name, "id": {platform: native_id}, "songs": songs}]


def builder(**kwargs):
    return handshake.get("settings", [])


def test(database, **kwargs):
    return True
