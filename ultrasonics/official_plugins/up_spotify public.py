#!/usr/bin/env python3

"""
up_spotify public

Input plugin for reading public Spotify playlists using Client Credentials
(app-only token, no user login, no OAuth redirect, no ultrasonics-api broker).

Use case: sync a public or editorial playlist into ultrasonics for output
to another service (e.g. Navidrome). Pair with up_time trigger for scheduled sync.

NOTE: Spotify's Nov-2024 / Feb-2026 API changes may restrict access to
Spotify-owned editorial/algorithmic playlists for apps in development mode.
User-created public playlists remain accessible. If a playlist returns 403,
the plugin logs the error clearly and raises so the applet reports failure.
"""

import re
import time

import requests

from ultrasonics import logs
from ultrasonics.tools import name_filter

log = logs.create_log(__name__)

handshake = {
    "name": "spotify public",
    "description": "read public spotify playlists using client credentials (no user login required)",
    "type": ["inputs"],
    "mode": ["playlists"],
    "version": "0.1",
    "settings": [
        {
            "type": "text",
            "label": "Client ID",
            "name": "client_id",
            "value": "From your Spotify Developer Dashboard app",
        },
        {
            "type": "text",
            "label": "Client Secret",
            "name": "client_secret",
            "value": "",
        },
        {
            "type": "string",
            "value": "This plugin uses Client Credentials (app-only) auth. No user login is needed. "
            "Note: Spotify-owned editorial playlists (e.g. 'Today's Top Hits') may be restricted "
            "for apps in development mode. User-created public playlists should work fine.",
        },
    ],
}

# Module-level token cache
_token_cache = {"access_token": None, "expires_at": 0}


def _get_token(client_id, client_secret):
    """
    Obtain or return a cached Client Credentials access token.
    Token is refreshed when it expires (typically 1 hour).
    """
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    log.info("Requesting new Spotify Client Credentials token...")
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=15,
    )

    if resp.status_code != 200:
        raise Exception(
            f"Failed to obtain Spotify token: {resp.status_code} {resp.text}"
        )

    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)

    log.info("Spotify Client Credentials token obtained.")
    return _token_cache["access_token"]


def _spotify_request(token, url, params=None):
    """Make an authenticated GET request to the Spotify Web API."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 401:
        raise Exception("Spotify token expired or invalid (401).")
    if resp.status_code == 403:
        raise Exception(
            f"Spotify returned 403 Forbidden for {url}. "
            "This playlist may be restricted for apps in development mode."
        )
    if resp.status_code == 404:
        raise Exception(f"Spotify playlist not found (404): {url}")

    resp.raise_for_status()
    return resp.json()


def _extract_playlist_id(url_or_id):
    """
    Extract a Spotify playlist ID from a URL, URI, or raw ID.
    Handles:
      - https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=...
      - spotify:playlist:37i9dQZF1DXcBWIGoYBM5M
      - 37i9dQZF1DXcBWIGoYBM5M
    """
    url_or_id = url_or_id.strip()

    # URL format
    match = re.search(r"playlist[/:]([a-zA-Z0-9]+)", url_or_id)
    if match:
        return match.group(1)

    # Raw ID (alphanumeric, 22 chars typical)
    if re.match(r"^[a-zA-Z0-9]+$", url_or_id):
        return url_or_id

    raise Exception(f"Could not extract playlist ID from: {url_or_id}")


def _track_to_songs_dict(item):
    """
    Convert a Spotify playlist track item to ultrasonics songs_dict format.
    item is the object from playlists/{id}/tracks response, with item["track"].
    """
    track = item.get("track")
    if not track or track.get("is_local"):
        return None

    title = track.get("name", "")
    artists = [artist["name"] for artist in track.get("artists", [])]

    album = None
    date = None
    if track.get("album"):
        album = track["album"].get("name")
        date = track["album"].get("release_date")

    isrc = None
    external_ids = track.get("external_ids", {})
    if external_ids:
        isrc = external_ids.get("isrc")

    song = {
        "title": title,
        "artists": artists,
    }

    if album:
        song["album"] = album
    if date:
        song["date"] = date
    if isrc:
        song["isrc"] = isrc
    if track.get("id"):
        song["id"] = {"spotify": str(track["id"])}

    # Remove empty values
    song = {k: v for k, v in song.items() if v}

    return song


def run(settings_dict, **kwargs):
    """
    Fetch a public Spotify playlist and return it in ultrasonics songs_dict format.

    settings_dict should contain "playlist_ids" (newline-separated URLs or IDs).
    """
    database = kwargs["database"]
    global_settings = kwargs["global_settings"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    client_id = database.get("client_id", "").strip()
    client_secret = database.get("client_secret", "").strip()

    if not client_id or not client_secret:
        raise Exception(
            "Spotify Public plugin not configured. Set Client ID and Client Secret in plugin settings."
        )

    token = _get_token(client_id, client_secret)

    # Parse playlist IDs from settings
    raw_ids = settings_dict.get("playlist_ids", "").strip()
    if not raw_ids:
        raise Exception("No playlist IDs/URLs provided in applet settings.")

    playlist_ids = [
        _extract_playlist_id(line)
        for line in raw_ids.splitlines()
        if line.strip()
    ]

    if not playlist_ids:
        raise Exception("Could not parse any valid playlist IDs from input.")

    songs_dict = []

    for playlist_id in playlist_ids:
        log.info(f"Fetching Spotify playlist: {playlist_id}")

        # Get playlist metadata
        playlist_data = _spotify_request(
            token, f"https://api.spotify.com/v1/playlists/{playlist_id}",
            params={"fields": "id,name,description,images"}
        )

        playlist_name = playlist_data.get("name", "Untitled")
        playlist_images = playlist_data.get("images", [])
        cover_url = playlist_images[0]["url"] if playlist_images else None

        log.info(f"Playlist: '{playlist_name}' ({playlist_id})")

        # Paginate tracks
        tracks = []
        url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
        params = {
            "fields": "items(track(id,name,artists(name),album(name,release_date),external_ids)),next,total",
            "limit": 100,
            "offset": 0,
        }

        while url:
            data = _spotify_request(token, url, params=params)

            items = data.get("items", [])
            for item in items:
                song = _track_to_songs_dict(item)
                if song:
                    tracks.append(song)

            # Follow pagination
            url = data.get("next")
            params = None  # next URL includes all params

        log.info(f"Fetched {len(tracks)} tracks from '{playlist_name}'.")

        playlist_entry = {
            "name": playlist_name,
            "id": {"spotify": playlist_id},
            "songs": tracks,
        }

        # Store cover art URL in the playlist dict for downstream reference
        # (Navidrome/Subsonic can't set this, but it's available for other outputs)
        if cover_url:
            playlist_entry["image"] = cover_url

        songs_dict.append(playlist_entry)

    return songs_dict


def test(database, **kwargs):
    """
    Validate that client credentials are correct by obtaining a token
    and fetching a known public resource.
    """
    client_id = database.get("client_id", "").strip()
    client_secret = database.get("client_secret", "").strip()

    if not client_id or not client_secret:
        raise Exception("Client ID and Client Secret are required.")

    token = _get_token(client_id, client_secret)

    # Quick validation: search for a track (always works with client credentials)
    resp = requests.get(
        "https://api.spotify.com/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": "test", "type": "track", "limit": 1},
        timeout=10,
    )

    if resp.status_code != 200:
        raise Exception(f"Token validation failed: {resp.status_code} {resp.text}")

    log.info("Spotify Client Credentials are valid.")


def builder(**kwargs):
    component = kwargs["component"]

    settings_dict = [
        {
            "type": "string",
            "value": "Enter one or more Spotify playlist URLs or IDs (one per line). "
            "These must be public playlists.",
        },
        {
            "type": "text",
            "label": "Playlist URLs / IDs",
            "name": "playlist_ids",
            "value": "e.g. https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        },
        {
            "type": "string",
            "value": "Note: Spotify-owned editorial playlists may not be accessible "
            "for apps in development mode. If you get a 403 error, try requesting "
            "Extended Quota Mode from your Spotify Developer Dashboard, or use a "
            "user-created public playlist instead.",
        },
    ]

    return settings_dict
